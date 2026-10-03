import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from arena_kernel import MatchObserver
import dashboard


class BridgeTests(unittest.TestCase):
    def test_disabled_never_creates_privileged_collector(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'ARENA_OBSERVER': '0'}), \
             patch('arena_kernel.KernelObserver') as collector:
            bridge = MatchObserver('test', {}, directory, lambda: None)
            bridge.start()
            self.assertEqual(bridge.status()['status'], 'disabled')
            collector.assert_not_called()
            self.assertIsNone(bridge.explain('agent-1', {}))
            bridge.stop()
            self.assertTrue(Path(directory, 'test.kernel.jsonl').is_file())

    def test_trace_loss_is_sticky_even_after_ready_message(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = MatchObserver('test', {}, directory, lambda: None)
            bridge._status({'status': 'ready', 'epoch': 'abc', 'boot_id': 'boot'})
            bridge._status({'status': 'degraded', 'loss_count': 1, 'message': 'lost events'})
            bridge._status({'status': 'ready'})
            self.assertTrue(bridge.failed)
            self.assertEqual(bridge.status()['status'], 'degraded')
            self.assertIsNone(bridge.explain('agent-1', {}))
            rows = [json.loads(line) for line in bridge.path.read_text().splitlines()]
            self.assertEqual(rows[-1]['status'], 'degraded')

    def test_loss_reported_during_shutdown_revokes_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = MatchObserver('test', {}, directory, lambda: None)
            bridge._status({'status': 'ready', 'loss_count': 0})
            bridge._status({'status': 'stopped', 'loss_count': 1})
            self.assertTrue(bridge.failed)
            self.assertIsNone(bridge.explain('agent-1', {'alive': False}))

    def test_old_match_callback_cannot_explain_current_death(self):
        bridge = Mock(match_id='old')
        state = {'match_id': 'new', 'players': {'agent-1': {}}, 'events': []}
        with patch.object(dashboard, 'KERNEL', bridge), patch.object(dashboard, 'STATE', state):
            report = dashboard.elimination_report('agent-1', {'alive': False, 'state': 'eliminated',
                        'death_at': 1, 'reason': 'Original session exited'})
        bridge.explain.assert_not_called()
        self.assertNotEqual(report['confidence'], 'confirmed')

    def test_confirmation_requires_observed_death_and_is_revoked_on_loss(self):
        bridge = Mock(match_id='match', failed=False)
        report = {'cause': 'attack', 'confidence': 'confirmed', 'attacker': 'agent-2',
                  'summary': 'Kernel chain', 'kernel_evidence': [], 'evidence_seqs': []}
        bridge.explain.return_value = report
        state = {'match_id': 'match', 'players': {'agent-1': {}, 'agent-2': {'name': 'Grok'}}, 'events': []}
        info = {'alive': True, 'state': 'standing'}
        with patch.object(dashboard, 'KERNEL', bridge), patch.object(dashboard, 'STATE', state):
            dashboard.elimination_report('agent-1', info)
            bridge.explain.assert_not_called()
            info.update(alive=False, state='eliminated', death_at=1, reason='Original session exited')
            confirmed = dashboard.elimination_report('agent-1', info)
            self.assertEqual(confirmed['confidence'], 'confirmed')
            info['elimination'] = confirmed
            bridge.failed = True
            bridge.explain.return_value = None
            revoked = dashboard.elimination_report('agent-1', info)
            self.assertNotEqual(revoked['confidence'], 'confirmed')
            self.assertIn('withdrawn', revoked['summary'])
