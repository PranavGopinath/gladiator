import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from session import run, turn_command


class SessionTests(unittest.TestCase):
    def test_normal_completion_resumes_same_conversation(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory) / 'calls'
            script = ("import json,sys; from pathlib import Path; "
                      f"p=Path({str(calls)!r}); "
                      "f=p.open('a'); f.write(json.dumps(sys.argv[1:])+'\\n'); f.close(); "
                      "print(json.dumps({'type':'system','subtype':'init','session_id':'fixture-session'}))")
            with contextlib.redirect_stdout(io.StringIO()):
                code = run([sys.executable, '-c', script, 'initial prompt'], 'claude', interval=0, max_turns=2)
            records = [json.loads(line) for line in calls.read_text().splitlines()]
            self.assertEqual(code, 0)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[1][:2], ['--resume', 'fixture-session'])

    def test_normal_nonzero_exit_preserves_session_and_schedules_next_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory) / 'calls'
            script = ("from pathlib import Path; import sys,json; "
                      f"p=Path({str(calls)!r}); p.open('a').write(json.dumps(sys.argv[1:])+'\\n'); "
                      "print(json.dumps({'type':'system','subtype':'init','session_id':'fixture-session'})); "
                      "sys.exit(23 if len(p.read_text().splitlines())==1 else 0)")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = run([sys.executable, '-c', script, 'prompt'], 'claude', interval=0, max_turns=2)
            self.assertEqual(code, 0)
            records = [json.loads(line) for line in calls.read_text().splitlines()]
            self.assertEqual(len(records), 2)
            self.assertEqual(records[1][:2], ['--resume', 'fixture-session'])
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            error = next(event for event in events if event.get('phase') == 'turn_error')
            self.assertEqual(error['error_category'], 'cli_exit')
            self.assertEqual(error['exit_code'], 23)

    def test_signal_kill_of_native_child_remains_fatal(self):
        script = 'import os,signal; os.kill(os.getpid(), signal.SIGKILL)'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = run([sys.executable, '-c', script, 'prompt'], 'claude', interval=0, max_turns=2)
        self.assertEqual(code, 137)
        event = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(event['phase'], 'failed')
        self.assertEqual(event['error_category'], 'model_signal')

    def test_initial_missing_id_retries_original_brief(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory) / 'calls'
            script = ("from pathlib import Path; import sys,json; "
                      f"p=Path({str(calls)!r}); p.open('a').write(json.dumps(sys.argv[1:])+'\\n'); "
                      "print('{}' if len(p.read_text().splitlines())==1 else "
                      "json.dumps({'type':'system','subtype':'init','session_id':'fixture-session'}))")
            with contextlib.redirect_stdout(io.StringIO()):
                code = run([sys.executable, '-c', script, 'original game brief'], 'claude', interval=0, max_turns=2)
            records = [json.loads(line) for line in calls.read_text().splitlines()]
            self.assertEqual(code, 0)
            self.assertEqual(len(records), 2)
            self.assertIn('original game brief', records[1][-1])
            self.assertNotIn('--resume', records[1])

    def test_explicit_policy_block_parks_without_retry_or_rephrasing(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory) / 'calls'
            script = ("from pathlib import Path; import sys,json; "
                      f"Path({str(calls)!r}).open('a').write('called\\n'); "
                      "print(json.dumps({'type':'turn.failed','error':{'message':"
                      "'This content was flagged for possible cybersecurity risk.'}})); sys.exit(1)")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = run([sys.executable, '-c', script, 'prompt'], 'claude', interval=0, max_turns=2)
            self.assertEqual(code, 0)
            self.assertEqual(calls.read_text().splitlines(), ['called'])
            event = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(event['phase'], 'blocked')
            self.assertIsNone(event['next_turn_at'])

    def test_resumed_session_never_switches_to_a_new_conversation(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory) / 'calls'
            script = ("from pathlib import Path; import sys,json; "
                      f"p=Path({str(calls)!r}); p.open('a').write(json.dumps(sys.argv[1:])+'\\n'); "
                      "identity='unexpected-new-session' if len(p.read_text().splitlines())==2 else 'original-session'; "
                      "print(json.dumps({'type':'system','subtype':'init','session_id':identity}))")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(run([sys.executable, '-c', script, 'brief'], 'claude', interval=0, max_turns=3), 0)
            records = [json.loads(line) for line in calls.read_text().splitlines()]
            self.assertEqual(records[1][:2], ['--resume', 'original-session'])
            self.assertEqual(records[2][:2], ['--resume', 'original-session'])
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            error = next(event for event in events if event.get('phase') == 'turn_error')
            self.assertEqual(error['error_category'], 'conversation_mismatch')
            self.assertEqual(error['session_id'], 'original-session')

    def test_codex_resume_uses_exact_thread(self):
        cmd = turn_command(['codex', 'exec', '--json', '--skip-git-repo-check', 'first'], 'codex', 'thread-123', 'next')
        self.assertEqual(cmd, ['codex', 'exec', 'resume', '--json', '--skip-git-repo-check', 'thread-123', 'next'])


if __name__ == '__main__':
    unittest.main()
