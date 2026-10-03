"""Opt-in real Docker SSH tracing check; no model/provider/credential calls.

Run: python3 tests/integration_kernel.py
Requires the arena computer image and kernel observer image/build prerequisites.
Only fixture containers receive signals. Observer stops before fixture cleanup.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kernel_evidence import EvidenceTracker
from kernel_observer import KernelObserver


def docker(*args, check=True, timeout=30):
    return subprocess.run(["docker", *args], text=True, capture_output=True,
                          check=check, timeout=timeout)


def run(output_dir=None):
    prefix = "arena-kernel-fixture-" + uuid.uuid4().hex[:12]
    network = prefix + "-net"
    victim, attacker = prefix + "-victim", prefix + "-attacker"
    output = Path(output_dir or Path(__file__).resolve().parents[1] / ".runs" / prefix)
    output.mkdir(parents=True, exist_ok=True)
    observer = None
    tracker = None
    events = []
    lock = threading.RLock()
    created = []
    image = "agent-arena-computer:local"

    def on_event(event):
        with lock:
            events.append(event)
            if tracker is not None:
                tracker.ingest(event)

    def report():
        with lock:
            tracker.set_health(observer.status())
            return tracker.explain("agent-2")

    def ssh(command):
        return docker("exec", attacker, "ssh", "-o", "StrictHostKeyChecking=no", "-o",
                      "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes", "-o",
                      "ConnectTimeout=5", "root@victim", command, check=False, timeout=15)

    try:
        docker("network", "create", "--internal", network)
        created.append(network)
        target = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(3600)"
        # Both containers use the same existing fixture SSH credentials in the
        # arena image. No host files, ports or secrets are mounted.
        docker("run", "-d", "--name", victim, "--network", network, "--network-alias", "victim",
               "--entrypoint", "sh", image, "-c",
               "mkdir -p /run/sshd; ssh-keygen -A >/dev/null; /usr/sbin/sshd -E /tmp/sshd.log; python3 -c '" + target + "' & target=$!; echo $target > /tmp/target.pid; wait $target")
        created.append(victim)
        docker("run", "-d", "--name", attacker, "--network", network,
               "--entrypoint", "sleep", image, "infinity")
        created.append(attacker)
        contestants = {}
        for player, container in (("agent-1", attacker), ("agent-2", victim)):
            info = json.loads(docker("inspect", container).stdout)[0]
            contestants[player] = {"container_id": info["Id"], "tracked_pid": info["State"]["Pid"]}
            if player == "agent-2":
                deadline = time.monotonic() + 10
                while docker("exec", container, "test", "-s", "/tmp/target.pid", check=False).returncode:
                    assert time.monotonic() < deadline, "Fixture target did not start"
                    time.sleep(.1)
                processes = docker("top", container, "-eo", "pid,comm").stdout.splitlines()[1:]
                python_pids = [int(row.split()[0]) for row in processes if row.split()[1] == "python3"]
                assert len(python_pids) == 1, processes
                contestants[player].update(tracked_pid=python_pids[0], supervisor_pid=info["State"]["Pid"])
        observer = KernelObserver(prefix, contestants, output, on_event=on_event)
        ids = observer.start()
        assert ids and observer.status()["status"] == "ready", observer.status()
        with lock:
            tracker = EvidenceTracker(ids)
            tracker.set_health(observer.status())
            for event in events:
                tracker.ingest(event)
        control = ssh("target=$(cat /tmp/target.pid); kill -TERM $target; sleep 0.3; kill -0 $target && echo IGNORED_TERM_STILL_ALIVE")
        assert control.returncode == 0 and "IGNORED_TERM_STILL_ALIVE" in control.stdout, control
        time.sleep(.4)
        assert report() is None, "An ignored TERM was falsely attributed as death"
        normal = docker("exec", victim, "sh", "-c", "exit 7", check=False)
        assert normal.returncode == 7, normal
        time.sleep(.4)
        assert report() is None, "Normal shell exit was falsely attributed as death"
        with lock:
            assert any(event.get("kind") == "EXIT" and event.get("exit_code") == 1792 for event in events), events
        ssh("kill -KILL $(cat /tmp/target.pid)")
        deadline = time.monotonic() + 10
        confirmed = None
        observed_dead = False
        while time.monotonic() < deadline:
            observed_dead = docker("inspect", "--format", "{{.State.Running}}", victim).stdout.strip() == "false"
            confirmed = report()
            if observed_dead and confirmed:
                break
            time.sleep(.2)
        assert observed_dead, "Docker did not observe the fixture contestant die"
        assert confirmed and confirmed["attacker"] == "agent-1", {"tracker": tracker.status, "observer": observer.status(), "events": events}
        assert confirmed["confidence"] == "confirmed" and confirmed["target"]["exit_code"] == 9, confirmed
        kinds = {event["type"] for event in confirmed["kernel_evidence"]}
        assert {"CONNECT", "SSH_RECV", "FORK", "SIGNAL", "EXIT"} <= kinds, confirmed
        (output / "events.json").write_text(json.dumps(events, indent=2))
        (output / "evidence.json").write_text(json.dumps(confirmed, indent=2))
        result = {"status": "passed", "ignored_term": True, "normal_exit": True,
                  "confirmed_attacker": "agent-1", "external_death": True, "output": str(output)}
        print(json.dumps(result))
        return result
    finally:
        if observer:
            observer.stop()
        for name in reversed(created):
            if name == network:
                cleanup = docker("network", "rm", name, check=False)
            else:
                cleanup = docker("rm", "-f", name, check=False)
            if cleanup.returncode:
                raise RuntimeError("Kernel fixture cleanup failed: " + cleanup.stderr.strip())
        if observer:
            remaining = docker("ps", "-a", "--filter", "name=^/" + observer.name + "$", "--format", "{{.ID}}")
            assert not remaining.stdout.strip(), "Observer was not removed"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Directory for captured kernel fixture evidence")
    run(parser.parse_args().output)
