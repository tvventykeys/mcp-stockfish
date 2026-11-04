import copy, json, os, pathlib, signal, subprocess, sys, tempfile, time, unittest, uuid
import chess
import worker
import shutil
from unittest import mock
ENGINE=os.environ.get('FISHY_TEST_ENGINE') or shutil.which('stockfish') or '/workspace/.tools/stockfish19/bin/stockfish'
HAS_ENGINE=pathlib.Path(ENGINE).is_file()
def request():
    return dict(schema=1,request_id=str(uuid.uuid4()),attempt_id=str(uuid.uuid4()),generation=0,job_id='prototype-only',expires_at=int(time.time())+120,initial_fen=chess.STARTING_FEN,moves=['e2e4','e7e5'],search_moves=[],time_ms=100,depth=8,multipv=1)
class Tests(unittest.TestCase):
    def assert_slot_available(self):
        import fcntl
        path=pathlib.Path(tempfile.gettempdir())/('fishy-task-slot-'+str(os.getuid()))
        with path.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            fcntl.flock(lock,fcntl.LOCK_UN)

    def test_slot_contention_is_rejected_before_engine_launch(self):
        import fcntl
        path=pathlib.Path(tempfile.gettempdir())/('fishy-task-slot-'+str(os.getuid()))
        with path.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            start=time.monotonic()
            with self.assertRaises(BlockingIOError): worker.run(request(),'/not-an-engine')
            self.assertLess(time.monotonic()-start,.5)
        self.assert_slot_available()

    def test_bad_inputs(self):
        for change in [dict(moves=['e2e5']),dict(moves=['0000']),dict(time_ms=999999),dict(depth=True),dict(expires_at=1),dict(search_moves=['e2e4']),dict(initial_fen='bad'),dict(extra='go infinite')]:
            with self.subTest(change=change),self.assertRaises(Exception):
                worker.validate(dict(request(),**change))
    @unittest.skipUnless(HAS_ENGINE,'set FISHY_TEST_ENGINE to a local Stockfish binary')
    def test_real_engine_and_fencing(self):
        r=request(); out=worker.run(r,ENGINE)
        self.assertIn('Stockfish',out['engine_name']); self.assertTrue(out['lines'][0]['pv'])
        for change in [dict(generation=1),dict(attempt_id=str(uuid.uuid4())),dict(moves=['d2d4'])]:
            with self.assertRaises(ValueError): worker.check(dict(r,**change),out)
        worker.check(r,out,out['engine_sha256'])
        with self.assertRaises(ValueError): worker.check(r,out,'0'*64)
        bad=copy.deepcopy(out); bad['lines'][0]['pv']=['e2e5']
        with self.assertRaises(ValueError): worker.check(r,bad)
    @unittest.skipUnless(HAS_ENGINE,'set FISHY_TEST_ENGINE to a local Stockfish binary')
    def test_black_to_move_normalization(self):
        r=request(); r['moves']=['e2e4']; out=worker.run(r,ENGINE)
        self.assertEqual(out['score_perspective'],'white'); worker.check(r,out)
    def test_cancel_releases_slot(self):
        with tempfile.NamedTemporaryFile() as f:
            with self.assertRaises(ValueError): worker.run(request(),ENGINE,f.name)
        self.assert_slot_available()
    def test_hung_engine_hard_deadline(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'hung'; p.write_text('#!/bin/sh\nsleep 60\n'); p.chmod(0o700)
            start=time.monotonic()
            with self.assertRaises(Exception): worker.run(request(),str(p))
            self.assertLess(time.monotonic()-start,9)
        self.assert_slot_available()
    @unittest.skipUnless(HAS_ENGINE,'set FISHY_TEST_ENGINE to a local Stockfish binary')
    def test_strict_json_and_result_schema(self):
        for raw in [b'{"a":1,"a":2}', b'{"cp":NaN}', b'{"cp":Infinity}', b'x'*(worker.MAX+1)]:
            with self.assertRaises(ValueError): worker.decode(raw)
        r=request(); out=worker.run(r,ENGINE)
        for mutate in [lambda x:x.update(schema=True),lambda x:x.update(generation=False),lambda x:x.update(extra='bad'),lambda x:x['settings'].update(threads=True),lambda x:x['lines'][0].update(cp=True,mate=None),lambda x:x['lines'][0].update(lowerbound=True,upperbound=True)]:
            bad=copy.deepcopy(out); mutate(bad)
            with self.assertRaises(ValueError): worker.check(r,bad)

    def test_fen_cannot_inject_commands(self):
        for fen in [chess.STARTING_FEN.replace(' ', '\n', 1),chess.STARTING_FEN+'\nquit',chess.STARTING_FEN.replace(' ', '\t', 1),chess.STARTING_FEN.replace(' ', '\r', 1)]:
            with self.assertRaises(ValueError): worker.validate(dict(request(),initial_fen=fen))

    def fake_engine(self, directory, extra):
        path=pathlib.Path(directory)/'fake-uci'
        text='#!'+sys.executable+'\nimport sys,time\nfor line in sys.stdin:\n line=line.strip()\n if line=="uci": print("id name test",flush=True); print("uciok",flush=True)\n elif line=="isready": print("readyok",flush=True)\n elif line.startswith("go "):\n'+extra
        path.write_text(text); path.chmod(0o700); return str(path)

    def test_black_bounds_are_reversed(self):
        with tempfile.TemporaryDirectory() as d:
            path=self.fake_engine(d,'  print("info depth 8 score cp 25 lowerbound pv e7e5",flush=True)\n  print("bestmove e7e5",flush=True)\n')
            r=request(); r['moves']=['e2e4']; out=worker.run(r,path)
            row=out['lines'][0]
            self.assertEqual(row['cp'],-25); self.assertFalse(row['lowerbound']); self.assertTrue(row['upperbound'])

    def test_oversized_engine_output_rejected_and_releases_slot(self):
        with tempfile.TemporaryDirectory() as d:
            path=self.fake_engine(d,'  print("x"*70000,flush=True)\n  time.sleep(60)\n')
            start=time.monotonic()
            with self.assertRaises(ValueError): worker.run(request(),path)
            self.assertLess(time.monotonic()-start,3)
        self.assert_slot_available()

    def test_supervisor_drains_large_result_before_wait(self):
        import fcntl
        cycles=['g1f3 g8f6 f3g1 f6g8','b1c3 b8c6 c3b1 c6b8','g1h3 g8h6 h3g1 h6g8']
        body=''
        for rank,cycle in enumerate(cycles,1):
            pv=' '.join([cycle]*64)
            body+='  print('+repr('info depth 8 multipv '+str(rank)+' score cp 0 pv '+pv)+',flush=True)\n'
        body+='  print("bestmove g1f3",flush=True)\n'
        original=subprocess.Popen
        def small_pipe(*args,**kwargs):
            p=original(*args,**kwargs)
            if p.stdout is not None: fcntl.fcntl(p.stdout.fileno(),fcntl.F_SETPIPE_SZ,4096)
            return p
        with tempfile.TemporaryDirectory() as d:
            path=self.fake_engine(d,body); r=request(); r['multipv']=3
            with mock.patch.object(worker.subprocess,'Popen',side_effect=small_pipe): out=worker.run(r,path)
            self.assertGreater(len(json.dumps(out)),4096)
            self.assertEqual(len(out['lines']),3)

    def test_active_cancel_and_sigterm_cleanup(self):
        for method in ['cancel','sigterm']:
            with self.subTest(method=method),tempfile.TemporaryDirectory() as d:
                r=request(); r['time_ms']=15000; r['depth']=30
                f=pathlib.Path(d)/'r.json'; f.write_text(json.dumps(r))
                heartbeat=pathlib.Path(d)/'heartbeat'; cancel=pathlib.Path(d)/'cancel'
                fake=pathlib.Path(d)/'fake'
                fake.write_text('#!'+sys.executable+'\nimport pathlib,time\np=pathlib.Path('+repr(str(heartbeat))+')\nwhile True:\n p.write_text(str(time.time_ns()))\n time.sleep(.05)\n')
                fake.chmod(0o700)
                p=subprocess.Popen([sys.executable,'worker.py','run',str(f),'--engine',str(fake),'--cancel-file',str(cancel)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                try:
                    deadline=time.monotonic()+3
                    while not heartbeat.exists() and time.monotonic()<deadline: time.sleep(.05)
                    self.assertTrue(heartbeat.exists())
                    if method=='cancel': cancel.touch()
                    else: p.terminate()
                    p.wait(timeout=2)
                    self.assertNotEqual(p.returncode,0)
                    time.sleep(.15); stamp=heartbeat.read_text(); time.sleep(.15)
                    self.assertEqual(stamp,heartbeat.read_text())
                finally:
                    if p.poll() is None: p.kill(); p.wait()
            self.assert_slot_available()

    def test_parent_death_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            r=request(); r['time_ms']=15000; r['depth']=30
            f=pathlib.Path(d)/'r.json'; f.write_text(json.dumps(r))
            heartbeat=pathlib.Path(d)/'heartbeat'
            fake=pathlib.Path(d)/'fake'
            fake.write_text('#!'+sys.executable+'\nimport pathlib,time\np=pathlib.Path('+repr(str(heartbeat))+')\nwhile True:\n p.write_text(str(time.time_ns()))\n time.sleep(.05)\n')
            fake.chmod(0o700)
            p=subprocess.Popen([sys.executable,'worker.py','run',str(f),'--engine',str(fake)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                deadline=time.monotonic()+3
                while not heartbeat.exists() and time.monotonic()<deadline: time.sleep(.05)
                self.assertTrue(heartbeat.exists())
                p.kill(); p.wait(); time.sleep(.4)
                stamp=heartbeat.read_text(); time.sleep(.2)
                self.assertEqual(stamp,heartbeat.read_text())
            finally:
                if p.poll() is None: p.kill(); p.wait()
        self.assert_slot_available()
if __name__=='__main__': unittest.main()
