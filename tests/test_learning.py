import copy
import io
import json
import os
from pathlib import Path
import random
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import arena_learning as learning
from arena_config import default_config, validate_config, compose_config
import dashboard

ROOT = Path(__file__).resolve().parents[1]


def config():
    return validate_config({'learning_enabled': True})


def history_row(identity='past', strategy='defense_first', reward=1, settings=None):
    settings = settings or config()
    return {'id': identity, 'rewards': {'agent-1': reward}, 'payload': {'decisions': [
        {'player_id': 'agent-1', 'strategy_id': strategy,
         'context': learning.context_for(settings, settings['players'][0])}]}}


class FakeStore:
    def __init__(self):
        self.records, self.finalized = {}, {}
        self.offline = False
        self.history_calls = 0
    def history(self, scope):
        self.history_calls += 1
        if self.offline:
            raise learning.LearningError('Fixture offline')
        return [dict(self.records[i], rewards=r['rewards']) for i, r in reversed(list(self.finalized.items()))
                if r['reason'] is None and self.records[i]['scope'] == scope][:200]
    def begin(self, record):
        self.records[record['id']] = copy.deepcopy(record)
    def finish(self, result):
        if self.offline:
            raise learning.LearningError('Fixture offline')
        self.finalized.setdefault(result['id'], copy.deepcopy(result))
        return {'status': 'skipped' if self.finalized[result['id']]['reason'] else 'scored',
                'checkpoint_id': len(self.finalized)}


class ControllerTests(unittest.TestCase):
    def test_empty_policy_explores_uniformly_and_records_probabilities(self):
        selected = learning.choose(config(), [], random.Random(5))
        self.assertEqual(len(selected['decisions']), 2)
        for decision in selected['decisions']:
            self.assertEqual(set(decision['distribution'].values()), {.25})
            self.assertEqual(decision['selection_probability'], .25)
            self.assertEqual(decision['controller_version'], selected['controller_version'])

    def test_reward_updates_context_and_shared_prior_without_starving_exploration(self):
        settings = config()
        state = learning.policy_state([history_row()])
        for player in settings['players']:
            probabilities, scores = learning.distribution(state, learning.context_for(settings, player))
            self.assertAlmostEqual(probabilities['defense_first'], .85)
            self.assertAlmostEqual(sum(probabilities.values()), 1)
            self.assertTrue(all(p >= .05 for p in probabilities.values()))
            self.assertEqual(scores['defense_first'], 1)

    def test_losses_and_ties_break_evenly(self):
        state = learning.policy_state([history_row(strategy='baseline', reward=.1)])
        probs, _ = learning.distribution(state, learning.context_for(config(), config()['players'][0]))
        self.assertAlmostEqual(probs['baseline'], .05)
        self.assertAlmostEqual(probs['defense_first'], probs['verification_first'])

    def test_whole_match_window_and_context_identity(self):
        state = learning.policy_state([history_row(str(i)) for i in range(201)])
        self.assertEqual(len(state['source_matches']), 200)
        settings = config(); old = learning.context_for(settings, settings['players'][0])
        settings['players'][0]['name'] = 'Renamed'
        self.assertEqual(old, learning.context_for(settings, settings['players'][0]))
        settings['players'][1]['model'] = 'different-model'
        self.assertNotEqual(old, learning.context_for(settings, settings['players'][0]))

    def test_scope_separates_objectives_and_catalogs(self):
        settings = config(); original = learning.scope_for(settings, ROOT)
        settings['prompt'] += '\nDifferent objective'
        self.assertNotEqual(original, learning.scope_for(settings, ROOT))
        with patch.object(learning, 'CATALOG', 'next-version'):
            self.assertNotEqual(original, learning.scope_for(config(), ROOT))

    def test_rewards_and_validity(self):
        state = {'outcome': 'winner', 'winner': 'a', 'players': {'a': {'alive': True}, 'b': {'alive': False}}}
        self.assertEqual(learning.outcome(state), ({'a': 1, 'b': .1}, None))
        state.update(outcome='draw', winner=None)
        state['players'] = {str(i): {'alive': i < 3} for i in range(5)}
        self.assertEqual(learning.outcome(state)[0], {'0': 1/3, '1': 1/3, '2': 1/3, '3': .1, '4': .1})
        for p in state['players'].values(): p['alive'] = False
        self.assertEqual(set(learning.outcome(state)[0].values()), {.1})
        for outcome in ('canceled', 'invalid', 'interrupted'):
            self.assertEqual(learning.outcome(dict(state, outcome=outcome))[0], {})
        self.assertEqual(learning.outcome(dict(state, learning_invalid=True))[0], {})
        state['players']['0']['error_category'] = 'policy_block'
        self.assertEqual(learning.outcome(state)[0], {})

    def test_disabled_and_baseline_compose_are_unchanged_for_all_harnesses(self):
        settings = config()
        settings['players'] = [
            {'id': str(i), 'name': str(i), 'harness': h, 'model': 'fixture',
             **({'base_url': 'http://host.docker.internal:1234/v1'} if h == 'compatible' else {})}
            for i, h in enumerate(('codex', 'claude', 'gemini', 'grok', 'compatible'))]
        original = compose_config(settings)
        decisions = {'decisions': [{'player_id': p['id'], 'instruction': ''} for p in settings['players']]}
        self.assertEqual(original, compose_config(settings, decisions))
        for d in decisions['decisions']: d['instruction'] = 'Fixture strategy'
        changed = compose_config(settings, decisions)
        for service in changed['services'].values():
            self.assertTrue(service['environment']['TASK'].endswith('Fixture strategy'))
        self.assertNotIn('SUPABASE', json.dumps(changed))
        self.assertFalse(default_config()['learning_enabled'])
        with self.assertRaises(ValueError): validate_config({'learning_enabled': 'true'})

    def test_provider_failures_latched_even_after_later_success(self):
        state = {'players': {'agent-1': {'harness': 'codex'}}, 'event_seq': 0, 'learning_invalid': False}
        with patch.object(dashboard, 'STATE', state), patch.object(dashboard, 'event'), patch.object(dashboard, 'refresh_eliminations'):
            dashboard.parse_log('agent-1', json.dumps({'type': 'arena.session', 'phase': 'turn_error', 'turn': 1, 'error_category': 'cli_exit'}))
            dashboard.parse_log('agent-1', json.dumps({'type': 'arena.session', 'phase': 'active', 'turn': 2}))
        self.assertTrue(state['learning_invalid'])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runs = Path(self.temp.name)
        self.store = FakeStore()
        self.service = learning.LearningService(ROOT, self.runs, lambda: self.store)
    def tearDown(self): self.temp.cleanup()
    def finished(self, identity, selected):
        return {'match_id': identity, 'learning': selected, 'outcome': 'winner', 'winner': 'agent-1',
                'players': {'agent-1': {'alive': True}, 'agent-2': {'alive': False}}}

    def test_full_cycle_restart_and_idempotent_outcome(self):
        selected = self.service.prepare('one', config())
        state = self.finished('one', selected)
        self.assertEqual(self.service.finalize(state)['status'], 'scored')
        self.service.finalize(state)
        self.assertEqual(len(self.store.finalized), 1)
        restarted = learning.LearningService(ROOT, self.runs, lambda: self.store)
        next_run = restarted.prepare('two', config())
        self.assertEqual(next_run['scored_matches'], 1)
        self.assertNotEqual(next_run['controller_version'], selected['controller_version'])
        winning = selected['decisions'][0]['strategy_id']
        self.assertGreater(next_run['decisions'][0]['estimated_rewards'][winning], .5)

    def test_failed_upload_retries_after_restart(self):
        selected = self.service.prepare('one', config())
        self.store.offline = True
        self.assertEqual(self.service.finalize(self.finished('one', selected))['status'], 'pending')
        self.assertEqual(len(self.service.pending()), 1)
        with self.assertRaises(learning.LearningError): self.service.prepare('two', config())
        self.store.offline = False
        restarted = learning.LearningService(ROOT, self.runs, lambda: self.store)
        restarted.retry()
        self.assertFalse(restarted.pending())
        self.assertEqual(json.loads((self.runs / 'one.learning.json').read_text())['status'], 'scored')

    def test_interrupted_match_is_skipped_on_recovery(self):
        self.service.prepare('one', config())
        self.service.recover_interrupted()
        self.assertTrue(self.store.finalized['one']['reason'])
        self.assertEqual(self.store.history(learning.scope_for(config(), ROOT)), [])

    def test_durable_outcome_wins_over_stale_selected_snapshot_after_crash(self):
        selected = self.service.prepare('one', config())
        writer = learning.atomic_json
        def fail_artifact(path, data):
            if path.name == 'one.learning.json':
                raise OSError('Fixture disk interruption')
            writer(path, data)
        with patch.object(learning, 'atomic_json', side_effect=fail_artifact):
            with self.assertRaises(OSError):
                self.service.finalize(self.finished('one', selected))
        restarted = learning.LearningService(ROOT, self.runs, lambda: self.store)
        restarted.recover_interrupted()
        restarted.retry()
        self.assertIsNone(self.store.finalized['one']['reason'])
        self.assertEqual(self.store.finalized['one']['rewards']['agent-1'], 1)

    def test_lost_registration_response_is_recovered_without_starting_match(self):
        begin = self.store.begin
        def lost_response(record):
            begin(record)
            raise learning.LearningError('Fixture lost response')
        with patch.object(self.store, 'begin', side_effect=lost_response):
            with self.assertRaises(learning.LearningError):
                self.service.prepare('one', config())
        self.service.retry()
        self.assertEqual(self.store.finalized['one']['reason'], 'Launch registration interrupted')
        self.assertFalse(list((self.runs / 'learning-registrations').glob('*.json')))

    def test_failed_prepare_does_not_launch_contestants(self):
        state = copy.deepcopy(dashboard.STATE)
        state['phase'] = 'finished'
        with patch.object(dashboard, 'STATE', state), patch.object(dashboard, 'check_credentials'), \
             patch.object(dashboard.LEARNING, 'prepare', side_effect=learning.LearningError('Fixture unavailable')), \
             patch.object(dashboard.threading, 'Thread') as launch:
            with self.assertRaises(learning.LearningError): dashboard.start_match(config())
        launch.assert_not_called()
        self.assertEqual(state['phase'], 'finished')

    def test_disabled_never_reads_credentials_or_calls_network(self):
        with patch.object(self.service, 'store_factory', side_effect=AssertionError('Must not load')):
            self.assertIsNone(self.service.prepare('off', default_config()))
            self.service.retry()

    def test_environment_loader_only_opens_fixture_file(self):
        fixture = self.runs / 'fixture.env'
        fixture.write_text('SUPABASE_URL="http://example.test"\nSUPABASE_SECRET_KEY=fixture-secret\nJEV_KEY=ignored\n')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(learning.runtime_settings(fixture)['SUPABASE_SECRET_KEY'], 'fixture-secret')
        with patch.dict(os.environ, {'SUPABASE_URL': 'http://example.test', 'SUPABASE_SECRET_KEY': 'fixture-secret'}), patch.object(Path, 'open', side_effect=AssertionError('Must not read')):
            self.assertEqual(learning.runtime_settings(Path('unused'))['SUPABASE_URL'], 'http://example.test')

    def test_transport_never_exposes_credentials_or_error_body(self):
        def fail(request, timeout):
            self.assertEqual(request.get_header('Apikey'), 'sb_secret_fixture')
            self.assertIsNone(request.get_header('Authorization'))
            raise urllib.error.HTTPError(request.full_url, 401, 'sb_secret_fixture', {}, io.BytesIO(b'sb_secret_fixture'))
        store = learning.SupabaseStore('https://example.test', 'sb_secret_fixture', fail)
        with self.assertRaises(learning.LearningError) as caught: store.history('fixture')
        self.assertNotIn('sb_secret_fixture', str(caught.exception))


if __name__ == '__main__': unittest.main()
