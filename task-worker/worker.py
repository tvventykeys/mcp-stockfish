"""Task-scoped Stockfish worker; never reads or writes Fishy Home storage."""
import argparse
import hashlib
import json
import os
import pathlib
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

MAX = 65536
MAX_LINE = 16384
REQUEST_KEYS = {
    'schema', 'request_id', 'attempt_id', 'generation', 'job_id', 'expires_at',
    'initial_fen', 'moves', 'search_moves', 'time_ms', 'depth', 'multipv',
}
RESULT_KEYS = {
    'schema', 'status', 'request_sha256', 'request_id', 'attempt_id', 'generation',
    'job_id', 'engine_sha256', 'engine_name', 'settings', 'score_perspective',
    'critical_evidence', 'lines',
}
LINE_KEYS = {'depth', 'cp', 'mate', 'pv', 'lowerbound', 'upperbound'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def number(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('integer outside bounds')


def decode(raw):
    if len(raw) > MAX:
        raise ValueError('oversized payload')

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('duplicate JSON key')
            out[key] = value
        return out

    def constant(_):
        raise ValueError('nonfinite JSON value')

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def load(path):
    with open(path, 'rb') as stream:
        return decode(stream.read(MAX + 1))


def _move(value):
    if type(value) is not str or len(value) not in (4, 5):
        raise ValueError('invalid move')
    if any(c not in 'abcdefgh12345678qrbn' for c in value):
        raise ValueError('invalid move')


def validate(request):
    import chess
    if (type(request) is not dict or set(request) != REQUEST_KEYS or
            type(request['schema']) is not int or request['schema'] != 1):
        raise ValueError('schema')
    for key in ('request_id', 'attempt_id'):
        if type(request[key]) is not str or str(uuid.UUID(request[key])) != request[key]:
            raise ValueError('UUID')
    number(request['generation'], 0, 2**31 - 1)
    number(request['expires_at'], 0, 2**53)
    now = time.time()
    if not now < request['expires_at'] <= now + 3600:
        raise ValueError('expiry')
    job = request['job_id']
    if type(job) is not str or not 1 <= len(job) <= 128 or any(ord(c) < 32 for c in job):
        raise ValueError('job ID')
    number(request['time_ms'], 100, 15000)
    number(request['depth'], 1, 30)
    number(request['multipv'], 1, 3)
    fen = request['initial_fen']
    if (type(fen) is not str or len(fen) > 128 or len(fen.split()) != 6 or
            any(ord(c) < 32 or ord(c) > 126 for c in fen)):
        raise ValueError('FEN')
    board = chess.Board(fen)
    if not board.is_valid():
        raise ValueError('board')
    for key, maximum in [('moves', 600), ('search_moves', 32)]:
        if type(request[key]) is not list or len(request[key]) > maximum:
            raise ValueError('moves')
        for move in request[key]:
            _move(move)
    for text in request['moves']:
        move = board.parse_uci(text)
        if move not in board.legal_moves:
            raise ValueError('illegal history')
        board.push(move)
    roots = [board.parse_uci(text) for text in request['search_moves']]
    if len(set(roots)) != len(roots) or any(move not in board.legal_moves for move in roots):
        raise ValueError('roots')
    if len(json.dumps(request, allow_nan=False).encode()) > MAX:
        raise ValueError('oversized request')
    return board, roots


def _kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def calculate(request, path):
    """Private child entry point; caller owns a separate process-group deadline."""
    board, roots = validate(request)
    digest_state = hashlib.sha256()
    with open(path, 'rb') as engine_file:
        for chunk in iter(lambda: engine_file.read(1024 * 1024), b''):
            digest_state.update(chunk)
    digest = digest_state.hexdigest()
    engine = subprocess.Popen([path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
    pending = b''
    deadline = min(time.monotonic() + request['time_ms'] / 1000 + 6,
                   time.monotonic() + request['expires_at'] - time.time())
    selector = selectors.DefaultSelector()
    selector.register(engine.stdout, selectors.EVENT_READ)

    def send(line):
        engine.stdin.write((line + '\n').encode())
        engine.stdin.flush()

    def until(token):
        nonlocal pending
        rows = []
        total_bytes = 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError('UCI deadline')
            while b'\n' in pending:
                if time.monotonic() >= deadline:
                    raise TimeoutError('UCI deadline')
                raw, pending = pending.split(b'\n', 1)
                if len(raw) > MAX_LINE:
                    raise ValueError('oversized engine line')
                line = raw.decode('utf-8', errors='strict').strip()
                total_bytes += len(raw)
                if total_bytes > 4 * MAX:
                    raise ValueError('oversized engine output')
                rows.append(line)
                if line == token or line.startswith(token + ' '):
                    return rows
            if not selector.select(min(.05, max(0, deadline - time.monotonic()))):
                continue
            data = os.read(engine.stdout.fileno(), 4096)
            if not data:
                raise ValueError('engine exited')
            pending += data
            if b'\n' not in pending and len(pending) > MAX_LINE:
                raise ValueError('oversized engine line')

    try:
        send('uci')
        banner = until('uciok')
        name = next((line[8:] for line in banner if line.startswith('id name ')), 'unknown')
        for option in ['Threads value 1', 'Hash value 64',
                       'MultiPV value ' + str(request['multipv'])]:
            send('setoption name ' + option)
        send('isready')
        until('readyok')
        # Send canonical board text, never raw caller-controlled UCI fragments.
        initial = board.root().fen(en_passant='fen')
        send('position fen ' + initial +
             (' moves ' + ' '.join(request['moves']) if request['moves'] else ''))
        send('go depth ' + str(request['depth']) + ' movetime ' + str(request['time_ms']) +
             (' searchmoves ' + ' '.join(request['search_moves']) if roots else ''))
        rows = until('bestmove')
        latest = {}
        for line in rows:
            fields = line.split()
            if not fields or fields[0] != 'info' or not all(k in fields for k in ('depth', 'score', 'pv')):
                continue
            depth = int(fields[fields.index('depth') + 1])
            index = fields.index('score')
            kind, value = fields[index + 1], int(fields[index + 2])
            if kind not in ('cp', 'mate'):
                continue
            lower, upper = 'lowerbound' in fields, 'upperbound' in fields
            if not board.turn:
                value, lower, upper = -value, upper, lower
            rank = int(fields[fields.index('multipv') + 1]) if 'multipv' in fields else 1
            number(rank, 1, request['multipv'])
            latest[rank] = dict(depth=depth, cp=value if kind == 'cp' else None,
                                mate=value if kind == 'mate' else None,
                                pv=fields[fields.index('pv') + 1:],
                                lowerbound=lower, upperbound=upper)
        lines = [latest[key] for key in sorted(latest)]
        if not lines and board.is_game_over():
            lines = [dict(depth=0, cp=None if board.is_checkmate() else 0,
                          mate=0 if board.is_checkmate() else None, pv=[],
                          lowerbound=False, upperbound=False)]
        return dict(schema=1, status='complete', request_sha256=fingerprint(request),
                    **{k: request[k] for k in ('request_id', 'attempt_id', 'generation', 'job_id')},
                    engine_sha256=digest, engine_name=name,
                    settings=dict(threads=1, hash_mb=64, multipv=request['multipv']),
                    score_perspective='white', critical_evidence=False, lines=lines)
    finally:
        selector.close()
        engine.kill()
        engine.wait()
        engine.stdin.close()
        engine.stdout.close()


def check(request, result, expected_engine_sha256=None):
    import chess
    board, roots = validate(request)
    if (type(result) is not dict or set(result) != RESULT_KEYS or
            type(result['schema']) is not int or result['schema'] != 1 or
            result['status'] != 'complete' or result['request_sha256'] != fingerprint(request)):
        raise ValueError('request mismatch')
    for key in ('request_id', 'attempt_id', 'generation', 'job_id'):
        if type(result[key]) is not type(request[key]) or result[key] != request[key]:
            raise ValueError('stale result')
    if result['score_perspective'] != 'white' or result['critical_evidence'] is not False:
        raise ValueError('evidence type')
    expected = dict(threads=1, hash_mb=64, multipv=request['multipv'])
    if (type(result['settings']) is not dict or set(result['settings']) != set(expected) or
            any(type(result['settings'][k]) is not int or result['settings'][k] != v
                for k, v in expected.items())):
        raise ValueError('settings')
    sha = result['engine_sha256']
    if type(sha) is not str or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
        raise ValueError('engine identity')
    if expected_engine_sha256 is not None:
        if (type(expected_engine_sha256) is not str or len(expected_engine_sha256) != 64 or
                any(c not in '0123456789abcdef' for c in expected_engine_sha256)):
            raise ValueError('invalid pinned engine identity')
        if sha != expected_engine_sha256:
            raise ValueError('engine identity mismatch')
    name = result['engine_name']
    if type(name) is not str or not 1 <= len(name) <= 256 or any(ord(c) < 32 for c in name):
        raise ValueError('engine name')
    lines = result['lines']
    if type(lines) is not list or not 1 <= len(lines) <= request['multipv']:
        raise ValueError('lines')
    seen_roots = set()
    for row in lines:
        if type(row) is not dict or set(row) != LINE_KEYS:
            raise ValueError('line schema')
        number(row['depth'], 0, 255)
        if (type(row['cp']) is int) == (type(row['mate']) is int):
            raise ValueError('score')
        active = row['cp'] if type(row['cp']) is int else row['mate']
        absent = row['mate'] if type(row['cp']) is int else row['cp']
        number(active, -2**31, 2**31 - 1)
        if absent is not None:
            raise ValueError('score')
        if any(type(row[k]) is not bool for k in ('lowerbound', 'upperbound')) or (row['lowerbound'] and row['upperbound']):
            raise ValueError('bounds')
        if type(row['pv']) is not list or len(row['pv']) > 256:
            raise ValueError('PV')
        if not row['pv'] and not board.is_game_over():
            raise ValueError('empty PV')
        replay = board.copy(stack=True)
        for text in row['pv']:
            _move(text)
            move = replay.parse_uci(text)
            if move not in replay.legal_moves:
                raise ValueError('illegal PV')
            replay.push(move)
        if row['pv']:
            root = chess.Move.from_uci(row['pv'][0])
            if root in seen_roots or (roots and root not in roots):
                raise ValueError('root')
            seen_roots.add(root)
    if len(json.dumps(result, allow_nan=False).encode()) > MAX:
        raise ValueError('oversized result')
    return result


def run(request, path, cancel=None):
    """One task slot, one process group, hard execution deadline and bounded I/O."""
    import fcntl
    validate(request)
    if cancel and pathlib.Path(cancel).exists():
        raise ValueError('cancelled')
    lock_path = pathlib.Path(tempfile.gettempdir()) / ('fishy-task-slot-' + str(os.getuid()))
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(lock_path, flags, 0o600)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Install termination handler before spawning so all child lifetimes are covered.
        old = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        process = None
        selector = selectors.DefaultSelector()
        try:
            # Defer signals across spawn/assignment so cleanup always knows the child PID.
            old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
            try:
                process = subprocess.Popen(
                    [sys.executable, str(pathlib.Path(__file__).resolve()), '_child', path, str(os.getpid())],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=True)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            deadline = min(time.monotonic() + request['time_ms'] / 1000 + 7,
                           time.monotonic() + request['expires_at'] - time.time())
            payload = json.dumps(request, allow_nan=False).encode()
            # Input is bounded but may exceed an OS pipe buffer: drain stdout while sending.
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, 'input')
            selector.register(process.stdout, selectors.EVENT_READ, 'output')
            sent, raw = 0, bytearray()
            while selector.get_map():
                if cancel and pathlib.Path(cancel).exists():
                    raise ValueError('cancelled')
                if time.monotonic() >= deadline:
                    raise TimeoutError('hard deadline')
                for key, _ in selector.select(min(.025, max(0, deadline - time.monotonic()))):
                    if key.data == 'input':
                        try:
                            sent += os.write(key.fd, payload[sent:sent + 4096])
                        except BrokenPipeError:
                            raise ValueError('child exited') from None
                        if sent == len(payload):
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                    else:
                        data = os.read(key.fd, 4096)
                        if not data:
                            selector.unregister(key.fileobj)
                        else:
                            raw.extend(data)
                            if len(raw) > MAX:
                                raise ValueError('oversized result')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('hard deadline')
            process.wait(timeout=remaining)
            if process.returncode:
                raise ValueError('engine failure')
            return check(request, decode(raw))
        finally:
            selector.close()
            if process is not None:
                _kill_group(process.pid)
                process.wait()
                for stream in (process.stdin, process.stdout):
                    if stream and not stream.closed:
                        stream.close()
            signal.signal(signal.SIGTERM, old)


def _child(path, expected_parent):
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})
    # The supervisor passes its PID before spawn; do not adopt a reparented child.
    if os.getpid() != os.getpgrp():
        raise ValueError('private process group required')

    def guard():
        while True:
            if os.getppid() != expected_parent or expected_parent == 1:
                _kill_group(os.getpgrp())
            time.sleep(.05)

    threading.Thread(target=guard, daemon=True).start()
    result = calculate(decode(sys.stdin.buffer.read(MAX + 1)), path)
    print(json.dumps(result, allow_nan=False))


def main():
    if len(sys.argv) == 4 and sys.argv[1] == '_child':
        _child(sys.argv[2], int(sys.argv[3]))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    command = sub.add_parser('request')
    command.add_argument('--job-id', required=True)
    command.add_argument('--moves', nargs='*', default=[])
    command.add_argument('--time-ms', type=int, default=1000)
    command = sub.add_parser('run')
    command.add_argument('request')
    command.add_argument('--engine', required=True)
    command.add_argument('--cancel-file')
    command = sub.add_parser('check')
    command.add_argument('request')
    command.add_argument('result')
    command.add_argument('--engine-sha256')
    args = parser.parse_args()
    if args.action == 'request':
        import chess
        request = dict(schema=1, request_id=str(uuid.uuid4()), attempt_id=str(uuid.uuid4()),
                       generation=0, job_id=args.job_id, expires_at=int(time.time()) + 900,
                       initial_fen=chess.STARTING_FEN, moves=args.moves, search_moves=[],
                       time_ms=args.time_ms, depth=20, multipv=1)
        validate(request)
        print(json.dumps(request, indent=2))
    elif args.action == 'run':
        print(json.dumps(run(load(args.request), args.engine, args.cancel_file), indent=2))
    else:
        check(load(args.request), load(args.result), args.engine_sha256)
        print('Validated for inspection only; no Home checkpoint committed.')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as error:
        print('Worker failed: ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
