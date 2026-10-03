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

    def test_failed_model_is_not_restarted(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = Path(directory) / 'calls'
            script = f"from pathlib import Path; import sys; Path({str(calls)!r}).open('a').write('called\\n'); sys.exit(23)"
            with contextlib.redirect_stdout(io.StringIO()):
                code = run([sys.executable, '-c', script, 'prompt'], 'claude', interval=0, max_turns=2)
            self.assertEqual(code, 23)
            self.assertEqual(calls.read_text().splitlines(), ['called'])

    def test_codex_resume_uses_exact_thread(self):
        cmd = turn_command(['codex', 'exec', '--json', '--skip-git-repo-check', 'first'], 'codex', 'thread-123', 'next')
        self.assertEqual(cmd, ['codex', 'exec', 'resume', '--json', '--skip-git-repo-check', 'thread-123', 'next'])


if __name__ == '__main__':
    unittest.main()
