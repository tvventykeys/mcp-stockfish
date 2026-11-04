# Fishy task worker — prototype

Runs independent bounded Stockfish position searches on the Codex runtime while a task is active. It installs no server or Windows startup task, and never reads or writes Fishy Home job storage.

**Status: worker prototype, not connected Home integration.** Direct Windows-to-task ingress is unavailable in this runtime. See [CONNECTION.md](CONNECTION.md) for the supported outbound-pull design and remaining Home changes.

## Implemented

- Strict, size-limited structured JSON, legal position/history/root validation, canonical UCI position serialization.
- One engine slot per Linux runtime user; one thread, 64 MiB Hash, MultiPV 1–3; 100–15,000 ms search limit and depth target 1–30.
- Hard supervisor deadline of requested search time plus seven seconds, bounded also by request expiry. Engine output and individual lines are bounded and drained while the child runs.
- Isolated process groups, SIGTERM/Ctrl-C cancellation, cancel-file polling and supervisor-death cleanup. No stateful engine sessions shared across requests.
- Request/attempt/generation fencing, actual achieved depth, White-perspective cp/mate scores and correctly normalized bound flags, legal PV checks and executable SHA-256. The checker can pin an expected engine digest.

No authentication, network transport, Home scheduling adapter, critical completed-iteration evidence, NNUE attestation, persistent availability or durable exactly-once execution is implemented. A claimed executable digest is not execution authentication. File exchange relies on user-mediated transfer; do not accept files from an untrusted sender.

## Use in an active Codex task

Python 3.10+ and `chess==1.11.2`. Create an isolated virtual environment if needed. The Linux worker requires POSIX flock/process groups and must run through its CLI/main thread. Request creation/result checking can run on Windows, but no Windows integration is installed.

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python worker.py request --job-id prototype-only --moves e2e4 e7e5 --time-ms 1000 > request.json
.venv/bin/python worker.py run request.json --engine /absolute/path/to/stockfish > result.json
.venv/bin/python worker.py check request.json result.json
# Optional expected binary identity, obtained separately from a trusted artifact:
.venv/bin/python worker.py check request.json result.json --engine-sha256 <expected-sha256>
```

The task's restored engine is `/workspace/.tools/stockfish19/bin/stockfish`. The `--engine` argument is a trusted operator-controlled executable path, never an untrusted request field. It must point to a real Stockfish executable; no raw UCI is accepted from requests.

Requests expire after 15 minutes by default. The accepted expiry is no more than one hour in the future; the checker also rejects expired requests. For custom initial FEN, candidate moves, generation, target depth or MultiPV, edit a **new** request before execution and retain that exact request for checking. Inputs are standard chess only. A search limit is a budget, not a guarantee that target depth is reached.

`complete` means the bounded attempt returned a validated result, not that target depth/stability was achieved or that Home accepted it. These results deliberately declare `critical_evidence: false`; do not use them to satisfy critical verification.

## Cancellation and retry

Ctrl-C or SIGTERM to the supervising CLI cleans up its private process group. Alternatively pass `--cancel-file /absolute/path/cancel` and create that file to request cancellation. The child monitors supervisor loss. This is not Home gaming-pause integration. OS/kernel failure cannot be given an absolute application-level termination guarantee.

There is no durable attempt ledger. Repeating the same request may compute it twice. Never automatically retry ambiguous work or put returned data directly into existing Home checkpoints. Request IDs prevent mismatched acceptance, not distributed duplicate execution.

## Tests

```sh
FISHY_TEST_ENGINE=/absolute/path/to/stockfish .venv/bin/python -m unittest -v
```

The tests discover Stockfish from `FISHY_TEST_ENGINE`, PATH or the known task path. Real-engine tests explicitly skip if none is available. Fake-engine tests cover cancellation, deadlines, output limits, pipe draining and parent-loss cleanup. See [VALIDATION.md](VALIDATION.md) for observed results. These are worker tests, not Home-to-Codex end-to-end tests.

## Production boundary and rollback

Home remains sole scheduler and writer. Its existing jobs, IDs, checkpoints, source PGNs, completed results, Turtle policy and startup bridge are not changed. No Home deployment, hosted service or environment republish was performed.

Rollback of this prototype is simply to stop an owned run and remove this package/its isolated virtual environment. Do not touch Home containers or volumes.
