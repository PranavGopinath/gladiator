import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard


class RefereeTests(unittest.TestCase):
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
