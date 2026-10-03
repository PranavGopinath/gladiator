import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from arena_attribution import explain_elimination


def tool(seq, actor='agent-4', command="ssh root@agent-2 'kill -TERM 14'", status='completed',
         at=100, output='Session terminated.', code=0, duration=.4):
    return {'seq': seq, 'time': at, 'player': actor, 'kind': 'tool', 'text': command,
            'data': {'status': status, 'tool_id': 'call-kill', 'output': output,
                     'exit_code': code, 'duration_seconds': duration}}


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.players = {f'agent-{index}': {'name': f'Gladiator {index}', 'harness': 'grok' if index == 4 else 'codex'}
                        for index in range(1, 5)}
        self.info = {'alive': False, 'state': 'eliminated', 'death_at': 100,
                     'reason': 'Original session exited'}
        self.death = {'seq': 108, 'time': 100, 'player': 'referee', 'kind': 'elimination',
                      'text': 'Claude eliminated: Original session exited', 'data': {'contestant': 'agent-2'}}

    def explain(self, events, observed_at=102, info=None):
        return explain_elimination('agent-2', self.info if info is None else info, self.players, events, observed_at)

    def test_recorded_ssh_heredoc_kill_becomes_probable_with_late_completion(self):
        command = "timeout 12 setsid ssh $SSHOPT root@agent-2 'bash -s' << 'REMOTE'\n# Kill session\nkill -TERM -14 2>/dev/null || kill -TERM 14\ncurl http://127.0.0.1:8080/status\nREMOTE\necho LOCAL_EXIT:$?"
        start = tool(107, command=command, status='running', at=99.7, code=None, duration=None)
        end = tool(109, command=command, at=100.4, output='{"alive":false,"exit_code":-15}', duration=.7)
        self.assertEqual(self.explain([start, self.death], 100)['cause'], 'session_exited')
        result = self.explain([start, self.death, end])
        self.assertEqual((result['cause'], result['confidence'], result['attacker']), ('attack', 'probable', 'agent-4'))
        self.assertEqual(result['evidence_seqs'], [107, 108, 109])
        self.assertIn('Gladiator 4 (Grok)', result['summary'])
        self.assertIn('untrusted', result['summary'])

    def test_literal_ip_and_shell_wrapper_supported(self):
        info = dict(self.info, arena_ips=['172.20.0.5'])
        command = '/bin/bash -lc "ssh root@172.20.0.5 \'pkill -f /opt/arena/session.py\'"'
        self.assertEqual(self.explain([tool(1, command=command)], info=info)['attacker'], 'agent-4')

    def test_generic_last_error_does_not_override_attack(self):
        info = dict(self.info, last_error='Model process failed', error_phase='failed',
                    error_category='model_error', error_at=100)
        self.assertEqual(self.explain([tool(1)], info=info)['cause'], 'attack')

    def test_multiple_successful_attackers_remain_ambiguous(self):
        result = self.explain([tool(1), tool(2, actor='agent-3')])
        self.assertIsNone(result['attacker'])
        self.assertEqual(result['confidence'], 'unknown')
        self.assertEqual(result['evidence_seqs'], [1, 2])

    def test_self_action_and_local_hardening_do_not_name_attacker(self):
        commands = ['pkill sshd; echo agent-2', "ssh root@agent-2 'pkill sshd'",
                    "ssh root@agent-2 'kill -0 14'", "ssh root@agent-2 true; kill -TERM 14",
                    "echo \"ssh root@agent-2 'kill -TERM 14'\"", "ssh root@agent-20 'kill -TERM 14'",
                    "ssh root@$HOST 'kill -TERM 14'", "ssh root@agent-2 'echo kill -TERM 14'"]
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(self.explain([tool(1, command=command)])['attacker'])
        self.assertIsNone(self.explain([tool(1, actor='agent-2')])['attacker'])

    def test_unsuccessful_old_or_postdeath_commands_do_not_blame_attacker(self):
        candidates = [tool(1, code=255), tool(1, status='failed'), tool(1, output='Connection refused', code=0),
                      tool(1, at=84, duration=1), tool(1, at=103, duration=4), tool(1, at=101, duration=.2)]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertIsNone(self.explain([candidate], observed_at=104)['attacker'])

    def test_oom_is_observed_even_with_attack_report(self):
        result = self.explain([tool(1), self.death], info=dict(self.info, oom_killed=True))
        self.assertEqual((result['cause'], result['confidence'], result['attacker']), ('oom', 'observed', None))

    def test_reported_fatal_categories_require_failed_phase(self):
        for category, cause in [('model_signal', 'model_signal'), ('provider_error', 'provider_error'),
                                ('startup', 'startup_error'), ('configuration', 'startup_error')]:
            info = dict(self.info, error_phase='failed', error_at=99.9, error_category=category)
            result = self.explain([], info=info)
            self.assertEqual((result['cause'], result['confidence']), (cause, 'reported'))
            for phase in ('turn_error', 'blocked'):
                result = self.explain([], info=dict(info, error_phase=phase))
                self.assertEqual(result['cause'], 'session_exited')

    def test_stale_fatal_info_is_ignored(self):
        info = dict(self.info, error_phase='failed', error_at=70, error_category='provider_error', last_error='old fatal error')
        self.assertEqual(self.explain([], info=info)['cause'], 'session_exited')

    def test_legacy_provider_failure_requires_fatal_session_event(self):
        failure = {'seq': 121, 'time': 99.7, 'player': 'agent-2', 'kind': 'session',
                   'text': 'Provider returned an invalid model response; contestant will not restart.',
                   'data': {'phase': 'failed'}}
        result = self.explain([failure, self.death])
        self.assertEqual((result['cause'], result['confidence']), ('provider_error', 'reported'))
        self.assertEqual(result['evidence_seqs'], [108, 121])
        failure['data']['phase'] = 'turn_error'
        self.assertEqual(self.explain([failure])['cause'], 'session_exited')

    def test_legacy_policy_block_and_negative_signal_are_distinct(self):
        policy = {'seq': 210, 'time': 99.6, 'player': 'agent-2', 'kind': 'error',
                  'text': 'This content was flagged for possible cybersecurity risk.', 'data': {}}
        fatal = {'seq': 212, 'time': 99.8, 'player': 'agent-2', 'kind': 'session',
                 'text': 'Model process failed; contestant will not restart.', 'data': {'phase': 'failed'}}
        self.assertEqual(self.explain([policy, fatal])['cause'], 'policy_block')
        self.assertEqual(self.explain([policy])['cause'], 'session_exited')
        fatal['data']['exit_code'] = -15
        self.assertEqual(self.explain([policy, fatal])['cause'], 'model_signal')
        fatal['data']['exit_code'] = 1
        policy['time'] = 90
        self.assertEqual(self.explain([policy, fatal])['cause'], 'model_error')

    def test_external_elimination_required(self):
        for info in (dict(self.info, alive=True), {'alive': False, 'state': 'starting'}):
            self.assertEqual(self.explain([tool(1)], info=info)['cause'], 'unknown')

    def test_container_stop_is_observed_without_inventing_attacker(self):
        result = self.explain([], info=dict(self.info, reason='Container stopped'))
        self.assertEqual((result['cause'], result['confidence'], result['attacker']), ('container_stopped', 'observed', None))

    def test_inputs_are_immutable_and_attribution_events_not_recursive_evidence(self):
        rows = [tool(107), self.death, {'seq': 110, 'time': 100.5, 'player': 'referee', 'kind': 'attribution',
                                      'text': 'prior explanation', 'data': {'contestant': 'agent-2'}}]
        inputs = copy.deepcopy((self.info, self.players, rows))
        result = self.explain(rows)
        self.assertEqual(result['evidence_seqs'], [107, 108])
        self.assertEqual((self.info, self.players, rows), inputs)
        self.assertEqual(set(result), {'cause', 'confidence', 'attacker', 'evidence_seqs', 'summary'})


if __name__ == '__main__':
    unittest.main()
