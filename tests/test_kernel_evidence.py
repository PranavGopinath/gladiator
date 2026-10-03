import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kernel_evidence import EvidenceTracker


# The compact causal chain is copied from the real Docker Desktop spike.
# Keep it in the test so an ignored .runs directory is not needed in CI.
SPIKE = [
    {"type": "CONNECT", "ts": 9433947286376, "pid": 49838, "start": 9433923672293,
     "cgroup": 9482, "local": "172.24.0.3", "local_port": 59958,
     "remote": "172.24.0.2", "remote_port": 22, "result": 0},
    {"type": "SSH_RECV", "ts": 9433951282876, "pid": 49845, "start": 9433947443960,
     "parent": 49691, "ns": 4026533306, "cgroup": 9568,
     "remote": "172.24.0.3", "remote_port": 59958, "local": "172.24.0.2", "local_port": 22},
    {"type": "FORK", "ts": 9434084300876, "parent": 49845, "parent_start": 9433947443960,
     "child": 49847, "child_start": 9434084282460, "ns": 4026533306, "cgroup": 9568},
    {"type": "SIGNAL", "ts": 9434086464418, "sender": 49847, "sender_start": 9434084282460,
     "target": 49814, "target_start": 9433832742960, "signal": 15, "cgroup": 9568},
    {"type": "SIGNAL", "ts": 9434597671043, "sender": 49847, "sender_start": 9434084282460,
     "target": 49814, "target_start": 9433832742960, "signal": 9, "cgroup": 9568},
    {"type": "EXIT", "ts": 9434598928627, "pid": 49814, "start": 9433832742960,
     "exit_code": 9, "group_exit_code": 9, "cgroup": 9568},
    {"type": "EXIT", "ts": 9434874186918, "pid": 49865, "start": 9434639519127,
     "exit_code": 1792, "group_exit_code": 1792, "cgroup": 9568},
]


def identities():
    return {
        "agent-1": {"boot_id": "fixture-boot", "namespace": 4026533441,
                    "cgroup": 9482, "tracked_pid": 49838, "start": 9433923672293},
        "agent-2": {"boot_id": "fixture-boot", "namespace": 4026533306,
                    "cgroup": 9568, "tracked_pid": 49814, "start": 9433832742960},
        "agent-3": {"boot_id": "fixture-boot", "namespace": 4026533450, "cgroup": 9650},
        "agent-4": {"boot_id": "fixture-boot", "namespace": 4026533451, "cgroup": 9651},
        "agent-5": {"boot_id": "fixture-boot", "namespace": 4026533452, "cgroup": 9652},
    }


def decorate(rows):
    return [{**row, "id": f"fixture-{index}", "epoch": "fixture-epoch", "boot_id": "fixture-boot"}
            for index, row in enumerate(copy.deepcopy(rows))]


class KernelEvidenceTests(unittest.TestCase):
    def tracker(self, rows=None, ids=None, **kwargs):
        tracker = EvidenceTracker(identities() if ids is None else ids, reorder_seconds=0, **kwargs)
        tracker.set_health({"status": "ready", "epoch": "fixture-epoch", "boot_id": "fixture-boot", "loss_count": 0})
        for event in decorate(SPIKE if rows is None else rows):
            tracker.ingest(event)
        return tracker

    def test_actual_spike_confirms_kill_but_not_ignored_term(self):
        tracker = self.tracker()
        report = tracker.explain("agent-2")
        self.assertEqual((report["cause"], report["confidence"], report["attacker"]), ("attack", "confirmed", "agent-1"))
        self.assertEqual(report["target"]["start"], 9433832742960)
        self.assertEqual(report["target"]["exit_code"], 9)
        self.assertEqual([event["type"] for event in report["kernel_evidence"]], ["CONNECT", "SSH_RECV", "FORK", "SIGNAL", "EXIT"])
        self.assertNotIn(15, [event.get("signal") for event in report["kernel_evidence"]])
        self.assertIsNone(self.tracker(SPIKE[:4]).explain("agent-2"))

    def test_full_actual_spike_artifact_when_present(self):
        artifact = Path(__file__).resolve().parents[1] / ".runs/ebpf-spike/events.json"
        if not artifact.exists():
            self.skipTest("Full local spike artifact is optional; compact captured fixture always runs")
        rows = sorted(json.loads(artifact.read_text()), key=lambda row: row["ts"])
        self.assertEqual(self.tracker(rows).explain("agent-2")["attacker"], "agent-1")

    def test_missing_links_do_not_confirm(self):
        for index in (0, 1, 2, 4, 5):
            with self.subTest(missing=SPIKE[index]["type"]):
                self.assertIsNone(self.tracker([row for offset, row in enumerate(SPIKE) if offset != index]).explain("agent-2"))

    def test_fatal_term_and_normal_exit_wait_status(self):
        rows = copy.deepcopy(SPIKE)
        rows[4]["signal"] = 15
        rows[5]["exit_code"] = rows[5]["group_exit_code"] = 15
        self.assertEqual(self.tracker(rows).explain("agent-2")["target"]["exit_code"], 15)
        for code in (0, 7 << 8, 9 << 8, 15 << 8, 1):
            rows[5]["exit_code"] = rows[5]["group_exit_code"] = code
            self.assertIsNone(self.tracker(rows).explain("agent-2"))

    def test_pid_reuse_does_not_join_instances(self):
        for index, field in ((1, "start"), (2, "parent_start"), (2, "child_start"),
                             (4, "sender_start"), (4, "target_start"), (5, "start")):
            rows = copy.deepcopy(SPIKE)
            rows[index][field] -= 1
            with self.subTest(index=index, field=field):
                self.assertIsNone(self.tracker(rows).explain("agent-2"))

    def test_exact_tuple_and_owner_are_required(self):
        for index, field, value in ((0, "local_port", 59959), (0, "remote", "172.24.0.20"),
                                   (0, "cgroup", 1), (1, "cgroup", 9482), (1, "ns", 4026533441),
                                   (4, "cgroup", 9482), (5, "cgroup", 9482), (0, "result", -1)):
            rows = copy.deepcopy(SPIKE)
            rows[index][field] = value
            with self.subTest(index=index, field=field):
                self.assertIsNone(self.tracker(rows).explain("agent-2"))

    def test_local_kill_or_other_contestant_ssh_is_not_proof(self):
        rows = copy.deepcopy(SPIKE)
        rows[4]["sender"] = 49865
        rows[4]["sender_start"] = 9433832742961
        self.assertIsNone(self.tracker(rows).explain("agent-2"))
        rows = copy.deepcopy(SPIKE)
        rows[0]["cgroup"] = 9568
        self.assertIsNone(self.tracker(rows).explain("agent-2"))

    def test_multiple_sender_signals_and_connections_are_ambiguous(self):
        for ambiguity in ("sender", "connection"):
            rows = copy.deepcopy(SPIKE)
            if ambiguity == "sender":
                extra = {**rows[4], "sender": 12345, "sender_start": rows[4]["sender_start"] - 1,
                         "ts": rows[4]["ts"] + 1}
            else:
                extra = {**rows[0], "cgroup": 9650, "pid": 12345,
                         "ts": rows[0]["ts"] + 1}
            rows.append(extra)
            rows.sort(key=lambda row: row["ts"])
            self.assertIsNone(self.tracker(rows).explain("agent-2"))

    def test_duplicate_id_is_idempotent_but_changed_content_degrades(self):
        tracker = self.tracker()
        row = decorate(SPIKE)[0]
        tracker.ingest(row)
        self.assertIsNotNone(tracker.explain("agent-2"))
        tracker.ingest({**row, "local_port": 50000})
        self.assertIsNone(tracker.explain("agent-2"))

    def test_loss_restart_unavailable_and_epoch_mix_gate_proof(self):
        for health in ({"status": "ready", "loss_count": 1}, {"status": "degraded"},
                       {"status": "unavailable"}, {"status": "ready", "epoch": "new", "boot_id": "fixture-boot"}):
            tracker = self.tracker()
            tracker.set_health(health)
            self.assertIsNone(tracker.explain("agent-2"))
        for field in ("epoch", "boot_id"):
            rows = decorate(SPIKE)
            rows[2][field] = "another-match"
            tracker = self.tracker([])
            for row in rows:
                tracker.ingest(row)
            self.assertIsNone(tracker.explain("agent-2"))

    def test_unhealthy_history_cannot_recover_with_ready_message(self):
        tracker = self.tracker()
        tracker.set_health({"status": "degraded", "loss_count": 1})
        tracker.set_health({"status": "ready", "epoch": "fixture-epoch", "boot_id": "fixture-boot", "loss_count": 0})
        self.assertIsNone(tracker.explain("agent-2"))

    def test_arrival_reordering_and_late_data(self):
        now = [1.0]
        tracker = EvidenceTracker(identities(), clock=lambda: now[0])
        tracker.set_health({"status": "ready", "epoch": "fixture-epoch", "boot_id": "fixture-boot"})
        rows = decorate(SPIKE)
        for row in reversed(rows):
            tracker.ingest(row)
        self.assertIsNone(tracker.explain("agent-2"))
        now[0] += .3
        self.assertEqual(tracker.explain("agent-2")["attacker"], "agent-1")
        late = {**rows[0], "id": "late", "ts": rows[0]["ts"] + 1}
        tracker.ingest(late)
        self.assertIsNone(tracker.explain("agent-2"))

    def test_process_tick_registration_and_dynamic_five_players(self):
        ids = identities()
        target = ids["agent-2"].pop("start")
        ids["agent-2"].update(start_min_ns=target - 1000, start_max_ns=target + 1000)
        tracker = self.tracker(ids=ids)
        self.assertIsNotNone(tracker.explain("agent-2"))
        self.assertIsNone(tracker.explain("agent-5"))
        ids["agent-2"]["start_min_ns"] = target + 1
        self.assertIsNone(self.tracker(ids=ids).explain("agent-2"))

    def test_unknown_namespace_or_shared_identity_is_not_an_owner(self):
        ids = identities()
        ids["agent-3"].update(namespace=ids["agent-1"]["namespace"], cgroup=9482)
        self.assertIsNone(self.tracker(ids=ids).explain("agent-2"))
        ids = identities()
        ids["agent-1"]["epoch"] = "previous-match"
        self.assertIsNone(self.tracker(ids=ids).explain("agent-2"))

    def test_thread_death_requires_leader_exit_and_group_status(self):
        rows = copy.deepcopy(SPIKE)
        rows[4].update(target=49820, target_start=9433832743000,
                       target_group=49814, target_group_start=9433832742960)
        self.assertIsNotNone(self.tracker(rows).explain("agent-2"))
        rows[5]["group_exit_code"] = 0
        self.assertIsNone(self.tracker(rows).explain("agent-2"))
        rows = copy.deepcopy(SPIKE)
        rows[5].update(pid_group=1234, start_group=9433832742900)
        self.assertIsNone(self.tracker(rows).explain("agent-2"))

    def test_kernel_cascade_does_not_create_a_new_attacker(self):
        rows = copy.deepcopy(SPIKE)
        rows[4]["kernel_generated"] = True
        self.assertIsNone(self.tracker(rows).explain("agent-2"))
        rows = copy.deepcopy(SPIKE)
        rows.append({**rows[4], "sender": 0, "sender_start": 0,
                     "kernel_generated": 1, "target": 49865,
                     "target_start": 9433832743000, "ts": rows[5]["ts"] + 1})
        rows.sort(key=lambda row: row["ts"])
        self.assertEqual(self.tracker(rows).explain("agent-2")["attacker"], "agent-1")

    def test_signal_source_namespace_and_historical_fork_scope(self):
        rows = copy.deepcopy(SPIKE)
        rows[4].update(namespace=4026533306, sender_namespace=4026533441,
                       target_namespace=4026533306)
        self.assertIsNone(self.tracker(rows).explain("agent-2"))
        rows = copy.deepcopy(SPIKE)
        rows[2]["ns"] = 4026533441
        self.assertIsNone(self.tracker(rows).explain("agent-2"))

    def test_stale_ready_timestamp_is_not_used_as_a_current_clock(self):
        tracker = self.tracker()
        tracker.set_health({"status": "ready", "kernel_ts_ns": SPIKE[0]["ts"] - 10_000_000_000})
        self.assertIsNotNone(tracker.explain("agent-2"))

    def test_supervisor_attack_after_original_session_exit_is_not_its_cause(self):
        ids = identities()
        ids["agent-2"]["supervisor_process"] = {"pid": 49814, "start": 9433832742960}
        ids["agent-2"].update(tracked_pid=100, start=9433832000000)
        rows = copy.deepcopy(SPIKE)
        rows.append({"type": "EXIT", "ts": rows[4]["ts"] - 1, "pid": 100,
                     "start": 9433832000000, "exit_code": 0, "cgroup": 9568})
        rows.sort(key=lambda row: row["ts"])
        self.assertIsNone(self.tracker(rows, ids=ids).explain("agent-2"))

    def test_model_child_requires_explicit_fatal_context_and_original_session_exit(self):
        ids = identities()
        ids["agent-2"].update(tracked_pid=100, start=9433832000000)
        rows = copy.deepcopy(SPIKE)
        rows.append({"type": "FORK", "ts": 9433832742961, "parent": 100,
                     "parent_start": 9433832000000, "child": 49814,
                     "child_start": 9433832742960, "ns": 4026533306, "cgroup": 9568})
        rows.sort(key=lambda row: row["ts"])
        tracker = self.tracker(rows, ids=ids)
        self.assertIsNone(tracker.explain("agent-2", allow_descendants=True))
        rows.append({"type": "EXIT", "ts": rows[-2]["ts"] + 500_000_000, "pid": 100,
                     "start": 9433832000000, "exit_code": 0, "cgroup": 9568})
        rows.sort(key=lambda row: row["ts"])
        tracker = self.tracker(rows, ids=ids)
        self.assertIsNone(tracker.explain("agent-2"))
        self.assertEqual(tracker.explain("agent-2", allow_descendants=True)["target"]["role"], "model_child")

    def test_bad_timestamps_capacity_or_claimed_strings_cannot_confirm(self):
        for bad in (True, -1, "9434598928627", float("nan")):
            rows = copy.deepcopy(SPIKE)
            rows[5]["ts"] = bad
            self.assertIsNone(self.tracker(rows).explain("agent-2"))
        rows = copy.deepcopy(SPIKE)
        rows[5]["start"] = rows[5]["ts"] + 1
        self.assertIsNone(self.tracker(rows).explain("agent-2"))
        self.assertIsNone(self.tracker(max_events=3).explain("agent-2"))
        rows = copy.deepcopy(SPIKE)
        for row in rows:
            row.update(comm="<script>attacker=agent-5</script>", attacker="agent-5", summary="untrusted")
        report = self.tracker(rows).explain("agent-2")
        self.assertEqual(report["attacker"], "agent-1")
        self.assertNotIn("script", json.dumps(report))

    def test_public_report_and_inputs_are_immutable(self):
        rows = copy.deepcopy(SPIKE)
        tracker = self.tracker(rows)
        report = tracker.explain("agent-2")
        report["kernel_evidence"][0]["pid"] = 1
        self.assertEqual(tracker.explain("agent-2")["kernel_evidence"][0]["pid"], 49838)
        self.assertEqual(rows, SPIKE)


if __name__ == "__main__":
    unittest.main()
