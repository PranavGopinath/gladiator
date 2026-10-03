import json
import unittest
from unittest.mock import patch
import dashboard


class EliminationUpdateTests(unittest.TestCase):
    def test_late_tool_result_enriches_death_without_reviving_contestant(self):
        command = "ssh root@agent-2 'kill -TERM 14'"
        state = {'match_id': None, 'event_seq': 1, 'events': [
            {'seq': 1, 'time': 105, 'player': 'agent-4', 'kind': 'tool', 'text': command,
             'data': {'tool_id': '1:x', 'status': 'running'}}],
            'players': {'agent-2': {'name': 'Claude', 'harness': 'claude', 'alive': True},
                        'agent-4': {'name': 'Grok', 'harness': 'grok', 'alive': True, 'turn': 1}}}
        with patch.object(dashboard, 'STATE', state), patch.object(dashboard, 'credential_values', return_value=()):
            with patch.object(dashboard.time, 'time', return_value=106):
                dashboard.apply_observations([('agent-2', {'alive': False, 'reason': 'Original session exited'})])
            self.assertIsNone(state['players']['agent-2']['elimination']['attacker'])
            with patch.object(dashboard.time, 'time', return_value=106.4):
                dashboard.parse_log('agent-4', json.dumps({'type': 'arena.tool', 'tool_id': 'x',
                    'name': command, 'status': 'completed', 'output': '', 'exit_code': 0}))
            victim = state['players']['agent-2']
            self.assertFalse(victim['alive'])
            self.assertEqual(victim['death_at'], 106)
            self.assertEqual(victim['elimination']['attacker'], 'agent-4')
            self.assertEqual(victim['elimination']['confidence'], 'probable')
            self.assertTrue(any(e['kind'] == 'attribution' for e in state['events']))
            self.assertEqual(victim['elimination']['evidence_seqs'], [1, 2, 3])
