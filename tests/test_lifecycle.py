import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from supervisor import Agent, handler_for, launch_options
from http.server import ThreadingHTTPServer


class LifecycleTests(unittest.TestCase):
    def test_codex_saved_login_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "codex"
            seed = Path(folder) / "seed.json"
            seed.write_text('{"tokens": {"access_token": "test-fixture"}}')
            with patch.dict(os.environ, {"AGENT": "codex", "CODEX_HOME": str(home),
                                        "CODEX_AUTH_FILE": str(seed), "CODEX_API_KEY": ""}):
                options = launch_options()
                self.assertNotIn("CODEX_API_KEY", options["env"])
                auth = home / "auth.json"
                self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
                auth.write_text('{"tokens": {"access_token": "refreshed-test-fixture"}}')
                launch_options()
                self.assertIn("refreshed-test-fixture", auth.read_text())

    def test_claude_secret_is_in_child_environment_only(self):
        with tempfile.TemporaryDirectory() as folder:
            secret = Path(folder) / "key"
            secret.write_text("test-fixture-only\n")
            with patch.dict(os.environ, {"AGENT": "claude", "ANTHROPIC_API_KEY_FILE": str(secret)}, clear=True):
                options = launch_options()
                self.assertEqual(options["env"]["ANTHROPIC_API_KEY"], "test-fixture-only")
                self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
                self.assertEqual(options["user"], "node")

    def start_agent(self, command):
        agent = Agent()
        with patch.dict(os.environ, {"AGENT": "custom", "AGENT_COMMAND": command}):
            agent.start()
        self.addCleanup(agent.stop)
        return agent

    def test_completion_is_reaped_and_retains_exit_status(self):
        agent = self.start_agent(f'{sys.executable} -c "pass"')
        agent.process.wait(timeout=5)
        self.assertEqual(agent.status()["state"], "completed")
        self.assertEqual(agent.status()["exit_code"], 0)
        self.assertFalse(agent.status()["alive"])

    def test_crash_preserves_health_but_fails_readiness(self):
        agent = self.start_agent(f'{sys.executable} -c "import time; time.sleep(60)"')
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(agent))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(base + "/readyz") as response:
            self.assertEqual(response.status, 200)
        agent.process.send_signal(signal.SIGTERM)
        agent.process.wait(timeout=5)
        with urllib.request.urlopen(base + "/healthz") as response:
            status = json.load(response)
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["exit_code"], -signal.SIGTERM)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(base + "/readyz")
        self.assertEqual(caught.exception.code, 503)
        caught.exception.close()

    def test_missing_executable_is_start_failure(self):
        agent = self.start_agent("/does-not-exist/arena-agent")
        self.assertEqual(agent.status()["state"], "start_failed")
        self.assertFalse(agent.status()["alive"])

    def test_supervisor_handles_sigterm(self):
        env = dict(os.environ, AGENT="custom", HEALTH_PORT="0",
                   AGENT_COMMAND=f'{sys.executable} -c "import time; time.sleep(60)"')
        process = subprocess.Popen([sys.executable, "-u", "supervisor.py"], env=env,
                                   cwd=Path(__file__).resolve().parents[1],
                                   stdout=subprocess.PIPE, text=True)
        try:
            self.assertIn("Health server listening", process.stdout.readline())
            process.terminate()
            self.assertEqual(process.wait(timeout=8), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()


if __name__ == "__main__":
    unittest.main()
