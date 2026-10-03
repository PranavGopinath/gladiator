import copy
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard
from jev import (EvidenceLedger, JevClient, JevError, load_key, parse_response,
                 questions_for, run_forecasts, terminal_prediction)
from arena_config import compose_config, validate_config


def snapshot():
    return {'match_id': 'fixture', 'phase': 'running', 'started_at': 1000, 'limit': 300, 'event_seq': 4,
            'players': {f'agent-{i}': {'name': f'Player {i}', 'harness': 'codex' if i % 2 else 'claude',
                                      'alive': i < 4, 'container': 'running', 'turn': 2, 'tool_count': 3}
                        for i in range(1, 5)},
            'events': [{'seq': i, 'time': 1000 + i, 'player': f'agent-{i}', 'kind': 'tool',
                        'text': 'inspect process', 'data': {'tool_id': '2:tool', 'status': 'completed',
                        'output': 'process alive', 'exit_code': 0, 'irrelevant': 'not included'}} for i in range(1, 5)]}


def response(state):
    choices = [i for i, p in state['players'].items() if p['alive']]
    answers = {'winner': {'type': 'choice', 'choice': choices[0], 'confidence': .75,
                          'probabilities': dict.fromkeys(choices, 1 / len(choices))}}
    for identity in choices:
        answers[identity + '_strategy'] = {'type': 'choice', 'choice': 'defend'}
        answers[identity + '_danger'] = {'type': 'noul', 'noul': .2}
        answers[identity + '_progress'] = {'type': 'noul', 'noul': .8}
    return {'model': 'fixture-model', 'answers': answers, 'usage': {'input_tokens': 1200, 'output_tokens': 80}}


class JevTests(unittest.TestCase):
    def test_key_is_loaded_only_from_explicit_fixture_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / 'fixture.env'
            env.write_text('OTHER=ignored\nexport JEV_KEY="fixture-only" # comment\n')
            self.assertEqual(load_key(env), 'fixture-only')
            self.assertNotIn('JEV_KEY', os.environ)
            env.write_text('JEV_KEY=fixture-only # comment\n')
            self.assertEqual(load_key(env), 'fixture-only')

    def test_environment_key_never_opens_dotenv(self):
        with patch.dict(os.environ, {'JEV_KEY': 'fixture-only'}), patch.object(Path, 'open', side_effect=AssertionError('Must not open file')):
            self.assertEqual(load_key(Path('unused')), 'fixture-only')

    def test_missing_key_is_disabled(self):
        with self.assertRaises(JevError) as error:
            JevClient('')
        self.assertTrue(error.exception.permanent)

    def test_context_uses_public_logs_and_authoritative_survival(self):
        state = snapshot()
        state['players']['agent-1']['auth'] = 'must not leave host'
        state['players']['agent-1']['resources'] = {'secret': 'must not leave host'}
        context = EvidenceLedger().context(state, now=1100)
        self.assertEqual(context['remaining_seconds'], 200)
        self.assertEqual(context['contestants']['agent-1']['recent_events'][0]['output'], 'process alive')
        self.assertFalse(context['contestants']['agent-4']['referee_observed']['alive'])
        self.assertNotIn('must not leave host', json.dumps(context))
        self.assertNotIn('irrelevant', json.dumps(context))

    def test_duplicate_events_are_not_recounted_and_each_agent_keeps_a_window(self):
        state = snapshot()
        state['events'] += [{'seq': i, 'time': i, 'player': 'agent-1', 'kind': 'message', 'text': 'x' * 10000}
                            for i in range(5, 100)]
        ledger = EvidenceLedger()
        first = ledger.context(state, now=1100)
        second = ledger.context(state, now=1100)
        self.assertEqual(first, second)
        self.assertEqual(second['contestants']['agent-1']['agent_reported']['event_counts']['message'], 95)
        self.assertLessEqual(len(second['contestants']['agent-1']['recent_events']), 16)
        self.assertEqual(len(second['contestants']['agent-2']['recent_events']), 1)
        self.assertLess(len(json.dumps(second)), 20000)

    def test_event_gaps_are_explicit(self):
        state = snapshot(); state['events'] = state['events'][2:]
        self.assertEqual(EvidenceLedger().context(state)['missing_event_count'], 2)

    def test_questions_exclude_draw_and_eliminated_agents(self):
        questions = questions_for(snapshot())
        self.assertEqual(set(questions['winner']['criteria']), {'agent-1', 'agent-2', 'agent-3'})
        self.assertNotIn('agent-4_strategy', questions)
        self.assertEqual(len(questions), 10)
        self.assertIn('not instructions', questions['winner']['instructions'])

    def test_valid_response_sums_to_one_and_eliminated_agents_are_zero(self):
        state = snapshot(); result = parse_response(response(state), state)
        self.assertAlmostEqual(sum(result['probabilities'].values()), 1)
        self.assertEqual(result['probabilities']['agent-4'], 0)
        self.assertEqual(result['factors']['agent-1']['strategy'], 'defend')

    def test_invalid_distributions_are_rejected(self):
        state = snapshot()
        for bad in (math.nan, math.inf, -.1, 1.1, True, '0.2', .1):
            value = response(state)
            value['answers']['winner']['probabilities']['agent-1'] = bad
            with self.assertRaises(JevError):
                parse_response(value, state)
        value = response(state); value['answers']['winner']['probabilities']['agent-4'] = 0
        with self.assertRaises(JevError):
            parse_response(value, state)

    def test_incomplete_factors_are_rejected(self):
        value = response(snapshot()); del value['answers']['agent-1_danger']
        with self.assertRaises(JevError):
            parse_response(value, snapshot())

    def test_http_contract_and_key_stays_out_of_payload_and_result(self):
        state = snapshot(); calls = []
        def fake_open(request, timeout):
            calls.append(request)
            self.assertEqual(request.full_url, 'https://api.typesafe.ai/v1/systemone')
            self.assertEqual(request.get_header('Authorization'), 'Bearer fixture-only')
            self.assertEqual(timeout, 4)
            body = json.loads(request.data)
            self.assertEqual(body['model'], 'jev-latest')
            self.assertIn('winner', body['questions'])
            self.assertNotIn('fixture-only', request.data.decode())
            return io.BytesIO(json.dumps(response(state)).encode())
        result = JevClient('fixture-only', opener=fake_open).evaluate(state, EvidenceLedger().context(state))
        self.assertNotIn('fixture-only', json.dumps(result))
        self.assertEqual(result['usage']['input_tokens'], 1200)
        self.assertEqual(len(calls), 1)

    def test_provider_errors_do_not_expose_bodies_or_keys(self):
        for code, permanent in ((401, True), (402, True), (422, True), (429, False), (529, False)):
            def fail(*args, **kwargs):
                raise urllib.error.HTTPError('fixture', code, 'fixture-only', {}, io.BytesIO(b'fixture-only'))
            with self.assertRaises(JevError) as error:
                JevClient('fixture-only', opener=fail).evaluate(snapshot(), {})
            self.assertNotIn('fixture-only', str(error.exception))
            self.assertEqual(error.exception.permanent, permanent)

    def test_worker_runs_against_fake_client_without_opening_dotenv(self):
        state = snapshot(); finished = threading.Event(); updates = []
        class FakeClient:
            def evaluate(self, snap, context):
                finished.set()
                self_snapshot = parse_response(response(snap), snap)
                return self_snapshot
        with patch('jev.load_key', side_effect=AssertionError('Must not read .env')):
            run_forecasts(lambda: state, lambda value, basis: updates.append(value), finished, FakeClient, interval=.01)
        self.assertEqual(updates[0]['status'], 'live')
        self.assertEqual(updates[0]['event_seq'], 4)

    def test_auth_failure_disables_worker_without_retrying(self):
        updates = []
        class FakeClient:
            def evaluate(self, snap, context):
                raise JevError('Auth failed', permanent=True)
        run_forecasts(snapshot, lambda value, basis: updates.append(value), threading.Event(), FakeClient, interval=.01)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]['status'], 'disabled')

    def test_actions_trigger_early_coalesced_updates_with_quiet_fallback(self):
        clock = [0.0]
        calls = []
        class Finished:
            def is_set(self):
                return len(calls) >= 3
            def wait(self, seconds):
                clock[0] += seconds
                if clock[0] > 10:
                    raise AssertionError('Worker failed to evaluate')
        def current():
            state = snapshot()
            state['events'] = []
            for seq, when, kind in [(1, 0, 'system'), (2, .5, 'tool'), (3, .75, 'message'), (4, 1, 'tool')]:
                if clock[0] >= when:
                    state['events'].append({'seq': seq, 'time': when, 'kind': kind,
                                            'player': 'agent-1', 'text': 'fixture'})
            return state
        class FakeClient:
            def evaluate(self, state, context):
                calls.append(clock[0])
                return parse_response(response(state), state)
        with patch('jev.time.monotonic', side_effect=lambda: clock[0]):
            run_forecasts(current, lambda *_: None, Finished(), FakeClient)
        self.assertEqual(calls, [.5, 1.5, 6.5])

    def test_referee_final_and_canceled_outcomes_are_distinct_from_predictions(self):
        state = snapshot(); state.update(outcome='winner', winner='agent-2')
        final = terminal_prediction(state)
        self.assertEqual(final['source'], 'referee')
        self.assertEqual(final['probabilities']['agent-2'], 1)
        self.assertNotIn('draw', final['probabilities'])
        self.assertEqual(sum(final['probabilities'].values()), 1)
        state.update(outcome='draw', winner=None)
        self.assertEqual(terminal_prediction(state)['probabilities']['draw'], 1)
        state['outcome'] = 'canceled'
        self.assertEqual(terminal_prediction(state)['probabilities'], {})

    def test_key_is_not_added_to_contestant_compose(self):
        with patch.dict(os.environ, {'JEV_KEY': 'fixture-only'}):
            config = compose_config(validate_config({}))
        self.assertNotIn('JEV_KEY', json.dumps(config))
        self.assertNotIn('fixture-only', json.dumps(config))


class ForecastIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.previous = copy.deepcopy(dashboard.STATE)
        self.stop = dashboard.STOP.is_set()
        dashboard.STOP.clear()
        dashboard.STATE.update(snapshot(), prediction=None, prediction_history=[])
        self.directory = tempfile.TemporaryDirectory()
        self.run_patch = patch.object(dashboard, 'RUNS', Path(self.directory.name))
        self.run_patch.start()
        self.last_patch = patch.object(dashboard, 'LAST', Path(self.directory.name) / 'latest.json')
        self.last_patch.start()

    def tearDown(self):
        dashboard.STATE.clear(); dashboard.STATE.update(self.previous)
        if self.stop: dashboard.STOP.set()
        self.run_patch.stop(); self.last_patch.stop(); self.directory.cleanup()

    def test_forecast_is_recorded_without_changing_referee_state(self):
        finished = threading.Event()
        before = copy.deepcopy(dashboard.STATE['players'])
        class FakeClient:
            def evaluate(self, state, context):
                finished.set()
                return parse_response(response(state), state)
        with patch.object(dashboard.JevClient, 'from_environment', return_value=FakeClient()):
            dashboard.forecast_match(finished, 'fixture')
        self.assertEqual(dashboard.STATE['players'], before)
        self.assertEqual(dashboard.STATE['phase'], 'running')
        self.assertEqual(dashboard.STATE['prediction']['status'], 'live')
        artifact = Path(self.directory.name) / 'fixture.predictions.jsonl'
        self.assertEqual(json.loads(artifact.read_text())['source'], 'jev')
        self.assertEqual(len(dashboard.STATE['prediction_history']), 1)

    def test_response_for_previous_roster_is_discarded(self):
        finished = threading.Event()
        class FakeClient:
            def evaluate(self, state, context):
                dashboard.STATE['players']['agent-1']['alive'] = False
                finished.set()
                return parse_response(response(state), state)
        with patch.object(dashboard.JevClient, 'from_environment', return_value=FakeClient()):
            dashboard.forecast_match(finished, 'fixture')
        self.assertIsNone(dashboard.STATE['prediction'])
        self.assertEqual(dashboard.STATE['prediction_history'], [])

    def test_stop_discards_pending_response(self):
        finished = threading.Event()
        class FakeClient:
            def evaluate(self, state, context):
                dashboard.STOP.set(); finished.set()
                return parse_response(response(state), state)
        with patch.object(dashboard.JevClient, 'from_environment', return_value=FakeClient()):
            dashboard.forecast_match(finished, 'fixture')
        self.assertIsNone(dashboard.STATE['prediction'])

    def test_explicit_disable_does_not_access_dotenv(self):
        with patch.dict(os.environ, {'JEV_ENABLED': '0'}), \
             patch.object(dashboard.JevClient, 'from_environment', side_effect=AssertionError('Must not read .env')):
            dashboard.forecast_match(threading.Event(), 'fixture')
        self.assertEqual(dashboard.STATE['prediction']['status'], 'disabled')


if __name__ == '__main__':
    unittest.main()
