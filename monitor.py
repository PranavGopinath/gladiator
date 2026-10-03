"""Host-side Docker observation plus untrusted in-container agent telemetry."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parent


def docker(*args):
    return subprocess.check_output(["docker", *args], cwd=ROOT, text=True, timeout=10)


def port_url(container, port):
    bindings = (container["NetworkSettings"].get("Ports") or {}).get(f"{port}/tcp") or []
    if not bindings:
        return None
    binding = bindings[0]
    host = binding["HostIp"]
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{binding['HostPort']}"


def observe(container):
    state = container["State"]
    health_url = port_url(container, 8080)
    row = {"time": datetime.now(timezone.utc).isoformat(), "id": container["Id"][:12],
           "name": container["Name"].lstrip("/"), "container": state["Status"],
           "oom_killed": state.get("OOMKilled", False), "container_exit_code": state.get("ExitCode"),
           "health_reachable": False, "agent_state": "unknown", "agent_exit_code": None,
           "health_url": health_url, "app_url": port_url(container, 8000)}
    if state["Running"] and not state.get("Paused") and health_url:
        try:
            with urllib.request.urlopen(health_url + "/status", timeout=1) as response:
                status = json.loads(response.read(65536))
            allowed = {"starting", "running", "completed", "failed", "start_failed"}
            if not isinstance(status, dict) or status.get("state") not in allowed:
                raise ValueError("Invalid telemetry")
            row.update(health_reachable=True, agent_state=status["state"],
                       agent_exit_code=status.get("exit_code"))
        except (OSError, ValueError):
            pass
    return row


def snapshot():
    ids = docker("compose", "ps", "--all", "--quiet").split()
    if not ids:
        return []
    containers = json.loads(docker("inspect", *ids))
    with ThreadPoolExecutor(max_workers=16) as pool:
        return list(pool.map(observe, containers))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--events", type=Path, help="Append each sample as JSONL")
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive")
    previous = {}
    try:
        while True:
            rows = snapshot()
            if not rows:
                print("No arena containers. Run: docker compose up -d", flush=True)
            if args.events:
                with args.events.open("a") as output:
                    for row in rows:
                        output.write(json.dumps(row) + "\n")
            for row in rows:
                key = (row["container"], row["health_reachable"], row["agent_state"], row["agent_exit_code"])
                if previous.get(row["id"]) != key or args.once:
                    print(f"{row['time']} {row['name']}: container={row['container']} "
                          f"health={'up' if row['health_reachable'] else 'down'} "
                          f"agent={row['agent_state']} exit={row['agent_exit_code']} "
                          f"app={row['app_url']} health_url={row['health_url']}", flush=True)
                previous[row["id"]] = key
            current = {row["id"] for row in rows}
            for removed in previous.keys() - current:
                print(f"Container {removed} removed", flush=True)
                del previous[removed]
            if args.once:
                return
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        # Don't print docker inspect/config output: it can contain environment secrets.
        raise SystemExit(f"Monitor unavailable ({type(exc).__name__}); check Docker Desktop and Compose.")


if __name__ == "__main__":
    main()
