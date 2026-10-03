import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard


class RefereeTests(unittest.TestCase):
    def roster(self, count):
        return {'players': {f'agent-{index}': {'name': f'Gladiator {index}', 'alive': True,
                                             'state': 'standing', 'activity': 'idle'}
                            for index in range(1, count + 1)}}

    def test_five_contestants_need_one_survivor_to_win(self):
        state = self.roster(5)
        state['prediction'] = {'status': 'live', 'probabilities': {'agent-2': 1.0}}
        with patch.object(dashboard, 'STATE', state), patch.object(dashboard, 'event') as emit:
            result = dashboard.apply_observations([
                ('agent-2', {'alive': False, 'reason': 'Original session exited'}),
                ('agent-3', {'alive': False, 'reason': 'Container stopped'}),
            ])
            self.assertIsNone(result)
            self.assertEqual(state['prediction']['status'], 'waiting')
            self.assertEqual(sum(info['alive'] for info in state['players'].values()), 3)
            result = dashboard.apply_observations([
                ('agent-1', {'alive': True, 'reason': None}),
                ('agent-4', {'alive': False, 'reason': 'Original session exited'}),
                ('agent-5', {'alive': False, 'reason': 'Original session exited'}),
            ])
        self.assertEqual(result, 'Gladiator 1 wins')
        self.assertTrue(state['players']['agent-1']['alive'])
        self.assertEqual(emit.call_count, 4)
        self.assertTrue(all(info['state'] == 'eliminated' for key, info in state['players'].items()
                            if key != 'agent-1'))

    def test_zero_survivors_is_draw(self):
        state = self.roster(5)
        with patch.object(dashboard, 'STATE', state), patch.object(dashboard, 'event') as emit:
            result = dashboard.apply_observations([
                (identity, {'alive': False, 'reason': 'Container stopped'})
                for identity in state['players']
            ])
        self.assertTrue(result.startswith('Draw'))
        self.assertFalse(any(info['alive'] for info in state['players'].values()))
        self.assertEqual(emit.call_count, 5)

    def test_eliminated_contestant_cannot_resurrect_or_change_evidence(self):
        state = self.roster(3)
        with patch.object(dashboard, 'STATE', state), patch.object(dashboard, 'event') as emit:
            with patch.object(dashboard.time, 'time', return_value=100):
                self.assertIsNone(dashboard.apply_observations([
                    ('agent-1', {'alive': False, 'reason': 'Original session exited', 'container': 'running'})
                ]))
            eliminated = dict(state['players']['agent-1'])
            with patch.object(dashboard.time, 'time', return_value=200):
                result = dashboard.apply_observations([
                    ('agent-1', {'alive': True, 'reason': None, 'state': 'standing'}),
                    ('agent-2', {'alive': False, 'reason': 'Container stopped'}),
                    ('agent-3', {'alive': True, 'reason': None}),
                ])
        self.assertEqual(result, 'Gladiator 3 wins')
        self.assertEqual(state['players']['agent-1'], eliminated)
        self.assertEqual(state['players']['agent-1']['death_at'], 100)
        self.assertEqual(emit.call_count, 2)

    def test_container_death_between_inspect_and_top_is_elimination(self):
        responses = [SimpleNamespace(returncode=0, stdout=json.dumps([{'State': state}]))
                     for state in ({'Running': True, 'Status': 'running'},
                                   {'Running': False, 'Status': 'exited', 'ExitCode': 137})]
        with patch.object(dashboard, 'command', side_effect=responses), \
             patch.object(dashboard, 'process_table', return_value=None):
            _, status = dashboard.observe('agent-1', {'container_id': 'test', 'tracked_pid': '42'})
        self.assertFalse(status['alive'])
        self.assertEqual(status['container_exit_code'], 137)

    def test_model_selection_is_independent_and_exact(self):
        models = dashboard.validate_models({'codex': 'gpt-6-sol', 'claude': 'claude-opus-4-8'})
        self.assertEqual(models['codex'], 'gpt-6-sol')
        self.assertEqual(models['claude'], 'claude-opus-4-8')

    def test_invalid_model_and_unknown_provider_are_rejected(self):
        for value in ({'claude': 'opus; touch /tmp/x'}, {'unknown': 'model'}, {'codex': ['model']}):
            with self.assertRaises(ValueError):
                dashboard.validate_models(value)

    def test_closed_health_port_is_not_elimination(self):
        response = SimpleNamespace(returncode=0, stdout=json.dumps([{'State': {'Running': True, 'Status': 'running'}}]))
        with patch.object(dashboard, 'command', return_value=response), \
             patch.object(dashboard, 'process_table', return_value=[{'pid': '42', 'status': 'S'}]), \
             patch.object(dashboard.urllib.request, 'urlopen', side_effect=OSError):
            _, status = dashboard.observe('codex', {'container_id': 'test', 'tracked_pid': '42', 'health_url': 'http://test'})
        self.assertTrue(status['alive'])
        self.assertEqual(status['health'], 'down')

    def test_replacement_process_does_not_restore_original(self):
        response = SimpleNamespace(returncode=0, stdout=json.dumps([{'State': {'Running': True, 'Status': 'running'}}]))
        with patch.object(dashboard, 'command', return_value=response), \
             patch.object(dashboard, 'process_table', return_value=[{'pid': '99', 'status': 'S'}]), \
             patch.object(dashboard.urllib.request, 'urlopen', side_effect=OSError):
            _, status = dashboard.observe('codex', {'container_id': 'test', 'tracked_pid': '42', 'health_url': 'http://test'})
        self.assertFalse(status['alive'])

    def test_internal_reasoning_is_not_forwarded_to_ui(self):
        with patch.object(dashboard, 'event') as emit:
            dashboard.parse_log('codex', json.dumps({'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'private'}}))
            dashboard.parse_log('claude', json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'thinking', 'thinking': 'private'}]}}))
        emit.assert_not_called()

    def test_credentials_are_redacted_from_tool_output(self):
        self.assertNotIn('sk-ant-test-fixture', dashboard.clean('value: sk-ant-test-fixture'))


if __name__ == '__main__':
    unittest.main()
