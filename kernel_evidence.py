"""Conservative correlation of observer-owned kernel events.

This module never decides whether a contestant has been eliminated. The referee
may use a report only after observing that contestant's original session die.
No command text, model output, host name, or process name is causal evidence.
"""

import copy
import heapq
import ipaddress
import math
import re
import time


def _int(value, minimum=1):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _token(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value))


def _process(event, prefix=""):
    pid = event.get(prefix or "pid")
    start = event.get(f"{prefix}_start" if prefix else "start")
    if prefix == "sender" and _int(pid, 0) and _int(start, 0) and pid == 0 and start == 0:
        # A kernel-origin signal has no userspace process instance. It can be
        # competing evidence, but can never establish a contestant attacker.
        return (0, 0)
    if _int(pid) and _int(start):
        return (pid, start)
    return None


def _tuple(event, receive=False):
    try:
        local = str(ipaddress.IPv4Address(event["local"]))
        remote = str(ipaddress.IPv4Address(event["remote"]))
    except (KeyError, ValueError, TypeError):
        return None
    lp, rp = event.get("local_port"), event.get("remote_port")
    if not (_int(lp) and _int(rp) and lp <= 65535 and rp <= 65535):
        return None
    return (remote, rp, local, lp) if receive else (local, lp, remote, rp)


class EvidenceTracker:
    """Track one collector epoch; loss or invalid history permanently gates proof.

    ``identities`` come from Docker and the privileged observer, not contestant
    status JSON. Process registration accepts exact ``start`` or the observer's
    half-open ``start_min_ns``/``start_max_ns`` /proc clock-tick interval.

    Events are settled after a bounded arrival delay to reorder cross-CPU output.
    An event older than the settled timestamp makes this epoch inconclusive.
    ``reorder_seconds=0`` is useful only for already sorted, offline fixtures.
    """

    SIGNAL_WINDOW_NS = 10_000_000_000
    CONNECT_WINDOW_NS = 30_000_000_000
    CHILD_FAILURE_WINDOW_NS = 2_000_000_000

    def __init__(self, identities=None, *, reorder_seconds=.25, max_events=100_000,
                 clock=time.monotonic):
        self.identities = copy.deepcopy(identities or {})
        self.reorder_seconds = max(0, float(reorder_seconds))
        self.max_events = max_events
        self._clock = clock
        self._events = []
        self._pending = []
        self._ids = {}
        self._sequence = 0
        self._watermark = 0
        self._epoch = None
        self._boot = None
        self._health = {"status": "unavailable"}
        self._failure = None

    @property
    def status(self):
        return {**self._health, "status": "degraded" if self._failure else self._health.get("status"),
                "message": self._failure or self._health.get("message", ""),
                "events": len(self._events), "pending": len(self._pending)}

    def register_identity(self, player, identity):
        """Register a dynamic contestant; changing an existing identity loses proof."""
        if player in self.identities and self.identities[player] != identity:
            self._fail("Contestant kernel identity changed during this collector epoch.")
        self.identities[player] = copy.deepcopy(identity)

    def register_target(self, player, process, *, role="session"):
        identity = self.identities.setdefault(player, {})
        identity[f"{role}_process"] = copy.deepcopy(process)

    def _fail(self, message):
        if self._failure is None:
            self._failure = message

    def set_health(self, health):
        self._health = copy.deepcopy(health) if isinstance(health, dict) else {"status": "unavailable"}
        epoch, boot = self._health.get("epoch"), self._health.get("boot_id")
        if _token(epoch) and _token(boot):
            if self._epoch is not None and (epoch, boot) != (self._epoch, self._boot):
                self._fail("Collector restarted; this match's kernel history is incomplete.")
            self._epoch, self._boot = epoch, boot
        if self._health.get("loss_count", 0) != 0:
            self._fail("The kernel collector lost events.")
        if self._health.get("status") == "degraded":
            self._fail("The kernel collector reported degraded coverage.")

    def ingest(self, event):
        """Accept only canonical observer events; returns no elimination decisions."""
        self._settle()
        if not isinstance(event, dict):
            self._fail("Invalid kernel event.")
            return []
        event = copy.deepcopy(event)
        kind = event.get("kind", event.get("type"))
        event["kind"] = kind
        event["ts"] = event.get("ts", event.get("ts_ns"))
        if kind in ("LOSS", "LOST", "ERROR"):
            self._fail("The kernel collector reported missing events.")
            return []
        if kind not in ("FORK", "CONNECT", "SSH_RECV", "SIGNAL", "EXIT"):
            self._fail("Unknown kernel event type.")
            return []
        if not (_token(event.get("id")) and _token(event.get("epoch"))
                and _token(event.get("boot_id")) and _int(event["ts"])):
            self._fail("Kernel event lacks a valid epoch, identity, or timestamp.")
            return []
        if self._epoch is None:
            self._epoch, self._boot = event["epoch"], event["boot_id"]
        if (event["epoch"], event["boot_id"]) != (self._epoch, self._boot):
            self._fail("Kernel events span different collector epochs or boots.")
            return []
        if event["id"] in self._ids:
            if event != self._ids[event["id"]]:
                self._fail("A kernel event identity was reused with different content.")
            return []
        fields = {"FORK": ("parent", "child"), "SIGNAL": ("sender", "target")}.get(kind, ("",))
        if any(_process(event, field) is None for field in fields):
            self._fail("Kernel event lacks a process instance identity.")
            return []
        if any(_process(event, field)[1] > event["ts"] for field in fields):
            self._fail("Kernel event timestamp predates its process instance.")
            return []
        if kind in ("CONNECT", "SSH_RECV") and _tuple(event, kind == "SSH_RECV") is None:
            self._fail("Kernel network event has an invalid socket tuple.")
            return []
        if kind == "SIGNAL" and not (_int(event.get("signal")) and event["signal"] <= 64):
            self._fail("Kernel event contains an invalid signal.")
            return []
        if kind == "EXIT" and not _int(event.get("exit_code"), 0):
            self._fail("Kernel event contains an invalid wait status.")
            return []
        if event["ts"] <= self._watermark:
            self._fail("Kernel events arrived outside the bounded reorder window.")
            return []
        if len(self._ids) >= self.max_events:
            self._fail("Kernel history capacity was exceeded.")
            return []
        self._ids[event["id"]] = event
        self._sequence += 1
        heapq.heappush(self._pending, (event["ts"], self._sequence, self._clock(), event))
        self._settle()
        return []

    def _settle(self):
        # A low timestamp arriving recently blocks newer records until its delay
        # expires; all output remains sorted in kernel time.
        now = self._clock()
        while self._pending and now - self._pending[0][2] >= self.reorder_seconds:
            ts, _, _, event = heapq.heappop(self._pending)
            self._events.append(event)
            self._watermark = ts

    def _owner(self, event, *, target=False):
        ns = event.get("namespace", event.get("pidns", event.get("ns")))
        if event.get("kind") == "SIGNAL":
            ns = event.get("target_namespace", ns) if target else event.get("sender_namespace", ns)
        cg = event.get("target_cgroup") if target else event.get("cgroup")
        owners = []
        for player, identity in self.identities.items():
            if identity.get("boot_id") != self._boot:
                continue
            if identity.get("epoch", self._epoch) != self._epoch:
                continue
            expected_ns = identity.get("namespace", identity.get("pidns"))
            expected_cg = identity.get("cgroup", identity.get("cgroup_id"))
            matched = False
            if _int(ns) and _int(expected_ns):
                if ns != expected_ns:
                    continue
                matched = True
            if _int(cg) and _int(expected_cg):
                if cg != expected_cg:
                    continue
                matched = True
            if matched:
                owners.append(player)
        return owners[0] if len(owners) == 1 else None

    @staticmethod
    def _matches(process, registration):
        if process is None or not isinstance(registration, dict):
            return False
        pid = registration.get("pid", registration.get("tracked_pid"))
        if process[0] != pid:
            return False
        exact = registration.get("start", registration.get("start_ns"))
        if _int(exact):
            return process[1] == exact
        low, high = registration.get("start_min_ns"), registration.get("start_max_ns")
        return _int(low, 0) and _int(high) and low <= process[1] < high

    def _registration(self, player, role="session"):
        identity = self.identities[player]
        if isinstance(identity.get(f"{role}_process"), dict):
            return identity[f"{role}_process"]
        if role == "session":
            return {**identity, "pid": identity.get("tracked_pid", identity.get("pid"))}
        nested = identity.get(role)
        if isinstance(nested, dict):
            return nested
        return {"pid": identity.get(f"{role}_pid"),
                "start": identity.get(f"{role}_start"),
                "start_min_ns": identity.get(f"{role}_start_min_ns"),
                "start_max_ns": identity.get(f"{role}_start_max_ns")}

    def _ancestry(self, process, at, forks):
        """Return historical ancestors, rejecting cycles/conflicting parentage."""
        result, visited = [(process, [])], {process}
        while process in forks:
            rows = [row for row in forks[process] if row["ts"] <= at]
            parents = {_process(row, "parent") for row in rows}
            if not parents:
                break
            if len(parents) != 1:
                return []
            row = min(rows, key=lambda item: item["ts"])
            parent = _process(row, "parent")
            if parent in visited:
                return []
            visited.add(parent)
            result.append((parent, result[-1][1] + [row]))
            process, at = parent, row["ts"]
        return result

    @staticmethod
    def _public(event):
        # Deliberately omit comm and any application-supplied strings.
        keys = ("id", "kind", "ts", "epoch", "boot_id", "pid", "start", "parent", "parent_start",
                "child", "child_start", "sender", "sender_start", "target", "target_start",
                "signal", "exit_code", "group_exit_code", "local", "local_port", "remote", "remote_port",
                "namespace", "ns", "sender_namespace", "target_namespace", "cgroup", "pid_group", "start_group", "sender_group", "sender_group_start",
                "target_group", "target_group_start", "signal_code", "kernel_generated")
        result = {key: event[key] for key in keys if key in event}
        result["type"] = event["kind"]
        return result

    def explain(self, player, tracked_process=None, *, allow_descendants=False, death_at=None):
        """Return a confirmed direct SSH signal chain, or ``None``.

        ``death_at`` optionally bounds collector arrival time near the referee's
        observation. A child is eligible only with explicit fatal-child context
        AND an observed original-session exit shortly after that child's death.
        A trusted exact ``tracked_process`` registration bypasses that inference.
        """
        self._settle()
        if self._failure or self._health.get("status") != "ready" or player not in self.identities:
            return None
        if not (_token(self._epoch) and _token(self._boot)):
            return None
        forks, exits, receives, connects, signals = {}, [], [], [], []
        for event in self._events:
            kind = event["kind"]
            if kind == "FORK":
                forks.setdefault(_process(event, "child"), []).append(event)
            elif kind == "EXIT":
                exits.append(event)
            elif kind == "SSH_RECV":
                receives.append(event)
            elif kind == "CONNECT" and event.get("result") == 0 and not isinstance(event.get("result"), bool):
                connects.append(event)
            elif kind == "SIGNAL":
                signals.append(event)
        session = self._registration(player)
        session_exits = [event for event in exits if self._matches(_process(event), session)]
        first_session_exit = min((event["ts"] for event in session_exits), default=None)
        reports = []
        for exit_event in exits:
            target = _process(exit_event)
            if self._owner(exit_event) != player:
                continue
            role = None
            if self._matches(target, tracked_process or session):
                role = "session"
            elif tracked_process is None and self._matches(target, self._registration(player, "supervisor")):
                if first_session_exit is None or exit_event["ts"] <= first_session_exit:
                    role = "supervisor"
            elif allow_descendants and tracked_process is None:
                parents = self._ancestry(target, exit_event["ts"], forks)
                if len(parents) >= 2 and self._matches(parents[1][0], session):
                    if first_session_exit is not None and 0 <= first_session_exit - exit_event["ts"] <= self.CHILD_FAILURE_WINDOW_NS:
                        role = "model_child"
            if role is None:
                continue
            received = exit_event.get("received_at")
            if death_at is not None and isinstance(received, (int, float)) and math.isfinite(received) and received > death_at + 1:
                continue
            raw_status = exit_event["exit_code"]
            fatal_signal = raw_status & 0x7f
            # Core bit is harmless, but upper exit-status bits denote normal exit.
            if fatal_signal not in (9, 15) or raw_status & ~0xff:
                continue
            # A nonleader's exit is not evidence that its whole process died.
            leader = exit_event.get("pid_group", target[0])
            leader_start = exit_event.get("start_group", target[1])
            if (leader, leader_start) != target:
                continue
            competing = []
            for signal in signals:
                direct = _process(signal, "target") == target
                group = (signal.get("target_group"), signal.get("target_group_start")) == target
                if (direct or group) and signal["signal"] == fatal_signal and 0 <= exit_event["ts"] - signal["ts"] <= self.SIGNAL_WINDOW_NS:
                    # Thread-directed signals require a matching group fatal status.
                    if not direct and exit_event.get("group_exit_code") != raw_status:
                        continue
                    competing.append(signal)
            if len({_process(row, "sender") for row in competing}) != 1:
                continue
            proofs = []
            for signal in competing:
                if signal.get("kernel_generated") or signal.get("signal_code") in (128,):
                    continue
                if role == "supervisor" and first_session_exit is not None and first_session_exit < signal["ts"]:
                    continue
                sender = _process(signal, "sender")
                if self._owner(signal) != player:
                    continue
                if any(_process(row) == sender and row["ts"] < signal["ts"] for row in exits):
                    continue
                ancestry = self._ancestry(sender, signal["ts"], forks)
                for ancestor, path in ancestry:
                    for receive in receives:
                        if _process(receive) != ancestor or self._owner(receive) != player:
                            continue
                        limit = min((row["ts"] for row in path), default=signal["ts"])
                        if receive["ts"] > limit:
                            continue
                        if any(self._owner(row) != player for row in path):
                            continue
                        candidates = [row for row in connects if _tuple(row) == _tuple(receive, True)
                                      and 0 <= receive["ts"] - row["ts"] <= self.CONNECT_WINDOW_NS]
                        if len(candidates) != 1:
                            continue
                        connection = candidates[0]
                        attacker = self._owner(connection)
                        if attacker is None or attacker == player:
                            continue
                        proofs.append((attacker, [connection, receive, *reversed(path), signal, exit_event]))
            if len({proof[0] for proof in proofs}) != 1:
                continue
            attacker, rows = min(proofs, key=lambda proof: (len(proof[1]), proof[1][-2]["ts"]))
            unique = {row["id"]: row for row in rows}
            reports.append({"cause": "attack", "confidence": "confirmed", "attacker": attacker,
                            "evidence_seqs": [], "summary": f"Kernel evidence links {attacker}'s SSH connection to SIG{('KILL' if fatal_signal == 9 else 'TERM')} and the tracked process's signal death.",
                            "mechanism": "ssh_signal", "target": {"pid": target[0], "start": target[1], "role": role,
                            "boot_id": self._boot, "epoch": self._epoch, "exit_code": raw_status},
                            "kernel_evidence": [self._public(row) for row in sorted(unique.values(), key=lambda row: row["ts"])]})
        if not reports or len({report["attacker"] for report in reports}) != 1:
            return None
        return min(reports, key=lambda report: report["kernel_evidence"][-1]["ts"])
