"""Launch one agent and report its lifecycle independently of its output."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def agent_command():
    kind = os.environ.get("AGENT", "demo")
    if kind == "demo":
        return ["python3", "-u", str(Path(__file__).with_name("demo.py"))]
    if kind == "custom":
        command = shlex.split(os.environ.get("AGENT_COMMAND", ""))
        if not command:
            raise ValueError("AGENT=custom requires AGENT_COMMAND")
        return command
    if kind in ("codex", "claude"):
        task = os.environ.get("TASK") or Path(__file__).with_name("task.txt").read_text()
        if kind == "codex":
            command = ["codex", "exec", "--json", "--skip-git-repo-check",
                       "--dangerously-bypass-approvals-and-sandbox"]
        else:
            command = ["claude", "--print", "--verbose", "--output-format", "stream-json",
                       "--dangerously-skip-permissions"]
            turns = os.environ.get("CLAUDE_MAX_TURNS", "8")
            if turns != "0":
                command += ["--max-turns", turns]
        if os.environ.get("MODEL"):
            command += ["--model", os.environ["MODEL"]]
        return command + [task]
    raise ValueError("AGENT must be demo, codex, claude, or custom")


def launch_options():
    """Read secrets at runtime; never bake them into the image or command argv."""
    kind = os.environ.get("AGENT", "demo")
    env = dict(os.environ)
    options = {"env": env}
    if kind == "codex":
        auth = Path(env.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
        seed = env.get("CODEX_AUTH_FILE")
        if seed and not auth.exists():
            contents = Path(seed).read_bytes()
            json.loads(contents)  # Reject malformed seeds before creating the cache.
            auth.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Preserve refreshed credentials on the named volume across restarts.
            fd = os.open(auth, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as target:
                target.write(contents)
        if not env.get("CODEX_API_KEY"):
            env.pop("CODEX_API_KEY", None)
            if not auth.exists():
                raise ValueError("Codex requires an API key or a saved login")
    elif kind == "claude":
        if env.get("ANTHROPIC_API_KEY_FILE"):
            env["ANTHROPIC_API_KEY"] = Path(env["ANTHROPIC_API_KEY_FILE"]).read_text().strip()
        if not env.get("ANTHROPIC_API_KEY"):
            raise ValueError("Claude requires an Anthropic API key")
        # Claude's bypass mode refuses uid 0. The image's node user can sudo
        # when machine administration is needed, while the supervisor stays root.
        env.update(HOME="/home/node", USER="node", LOGNAME="node")
        options.update(user="node", group="node", extra_groups=[])
    return options


class Agent:
    def __init__(self):
        self.started = time.time()
        self.process = None
        self.error = None
        self.stopping = False
        self.lock = threading.Lock()

    def start(self):
        try:
            command = agent_command()
            if os.environ.get("CONTINUOUS_SESSION") == "1":
                command = ["python3", "-u", str(Path(__file__).with_name("session.py"))]
            if os.environ.get("WAIT_FOR_START") == "1":
                command = ["python3", str(Path(__file__).with_name("gate.py")), *command]
            self.process = subprocess.Popen(command, start_new_session=True, **launch_options())
        except (OSError, ValueError) as exc:
            # Never include command arguments or credentials in the public response.
            self.error = type(exc).__name__
            print("Agent failed to start; check AGENT, AGENT_COMMAND, and credentials.", flush=True)

    def status(self):
        with self.lock:
            code = self.process.poll() if self.process else None
            state = ("start_failed" if self.error else "starting" if self.process is None
                     else "running" if code is None else "completed" if code == 0 else "failed")
            return {"agent": os.environ.get("AGENT", "demo"), "state": state,
                    "alive": state == "running", "pid": self.process.pid if self.process else None,
                    "exit_code": code, "uptime_seconds": round(time.time() - self.started, 2),
                    "stopping": self.stopping, "error": self.error}

    def stop(self):
        self.stopping = True
        if self.process:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
            except ProcessLookupError:
                pass


def handler_for(agent):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/healthz", "/readyz", "/status"):
                self.send_error(404)
                return
            status = agent.status()
            code = 503 if self.path == "/readyz" and not status["alive"] else 200
            body = json.dumps(status).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return Handler


def main():
    agent = Agent()
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("HEALTH_PORT", "8080"))), handler_for(agent))
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    agent.start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("Health server listening; agent lifecycle available at /status", flush=True)
    try:
        stop.wait()
    finally:
        agent.stop()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
