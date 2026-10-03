"""Experiment lifecycle tests use synthetic referee outcomes and an in-memory store."""
import copy
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import arena_experiments as experiments
import arena_learning as learning
from arena_config import default_config, validate_config

ROOT = Path(__file__).resolve().parents[1]


def config():
    value = default_config()
    for player in value['players']:
        player.update(harness='codex', model='fixture-model')
    return value


class Store:
    """Simulates lost replies as well as pre-commit failures; SQL tested separately."""
    def __init__(self):
        self.rows, self.calls, self.catalog, self.checkpoints = {}, [], [], {}
        self.offline = self.lose_reply = False

    def request(self, route, body=None):
        if self.offline:
            raise learning.LearningError('Offline fixture')
        if route == 'rpc/learning_checkpoint_catalog':
            return copy.deepcopy(self.catalog)
        if route.startswith('learning_checkpoints?'):
            key = int(route.split('eq.')[1].split('&')[0])
            return [copy.deepcopy(self.checkpoints[key])]
        if route.startswith('learning_experiments?'):
            key = route.split('eq.')[1].split('&')[0]
            return [copy.deepcopy(self.rows[key])] if key in self.rows else []
        assert route == 'rpc/learning_experiment_commit'
        body = copy.deepcopy(body)
        previous = self.rows.get(body['p_id'])
        revision = body['p_expected_revision'] + 1
        if previous and previous['revision'] == revision and previous['payload'] == body['p_payload']:
            return {'revision': revision}
        if previous and previous['revision'] != body['p_expected_revision']:
            raise learning.LearningError('Conflict')
        self.rows[body['p_id']] = {'payload': body['p_payload'], 'revision': revision}
        self.calls.append(body)
        if self.lose_reply:
            self.lose_reply = False
            raise learning.LearningError('Reply lost after commit')
        return {'revision': revision}


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runs = Path(self.temp.name)
        self.store = Store()
        self.service = experiments.ExperimentService(ROOT, self.runs, lambda: self.store)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, **kwargs):
        return self.service.create(config(), kwargs.pop('learner', 'agent-1'), **kwargs)

    def finish(self, identity, winner=None, outcome='winner', **kwargs):
        run = self.service.current['active_run']
        winner = winner or run['learner_id']
        return self.service.finalize({'match_id': identity, 'outcome': outcome, 'winner': winner,
            'started_at': 100, 'ended_at': 112.25, 'players': {
                p['player_id']: {'alive': p['player_id'] == winner} for p in run['learning']['decisions']}, **kwargs})

    def train(self, identity='train'):
        self.service.prepare(identity)
        return self.finish(identity)

    def test_one_sided_training_rotates_positions_and_freezes(self):
        self.create(learner='agent-2')
        for index in range(3):
            settings, selected, run = self.service.prepare('train-' + str(index))
            self.assertEqual(run['slot'], index % 2)
            self.assertEqual(settings['players'][run['slot']]['name'], config()['players'][1]['name'])
            learner, fixed = sorted(selected['decisions'], key=lambda d: not d['learnable'])
            self.assertEqual(learner['role'], 'learner')
            self.assertEqual(fixed['strategy_id'], 'baseline')
            self.assertFalse(fixed['learnable'])
            self.finish('train-' + str(index))
        policy = learning.policy_state(self.service.current['history'])
        self.assertEqual(sum(x['count'] for x in policy['pooled'].values()), 3)
        self.assertEqual(sum(x['total'] for x in policy['pooled'].values()), 3)
        view = self.service.freeze()['experiment']
        self.assertEqual(view['phase'], 'evaluation')
        self.assertEqual(self.service.current['frozen']['state'], policy)
        self.assertEqual(self.service.current['frozen']['source_rows'], self.service.current['history'])

    def test_eight_evaluations_are_balanced_and_never_update_learning(self):
        self.create(); self.train(); self.service.freeze()
        frozen = copy.deepcopy(self.service.current['frozen'])
        history = copy.deepcopy(self.service.current['history'])
        start = len(self.store.calls)
        for index in range(8):
            _, policy, run = self.service.prepare('eval-' + str(index))
            self.assertTrue(policy['evaluation_only'])
            self.assertTrue(all(not d['learnable'] and d['selection_probability'] == 1 for d in policy['decisions']))
            if run['arm'] == 'baseline':
                self.assertTrue(all(d['strategy_id'] == 'baseline' for d in policy['decisions']))
            self.finish('eval-' + str(index))
        self.assertEqual(self.service.current['phase'], 'complete')
        self.assertEqual(self.service.current['history'], history)
        self.assertEqual(self.service.current['frozen'], frozen)
        self.assertEqual(self.service.current['scored_training'], 1)
        for call in self.store.calls[start:]:
            self.assertIsNone(call['p_registration']); self.assertIsNone(call['p_result']); self.assertIsNone(call['p_checkpoint'])
        for arm in ('baseline', 'learned'):
            runs = [r for r in self.service.current['runs'] if r['arm'] == arm]
            self.assertEqual(sorted(r['slot'] for r in runs), [0, 0, 1, 1])
        with self.assertRaises(ValueError): self.service.prepare('ninth')

    def test_invalid_eval_retries_same_slot_and_counts_exclusion(self):
        self.create(); self.train(); self.service.freeze()
        planned = copy.deepcopy(experiments.next_slot(self.service.current))
        self.service.prepare('invalid')
        result = self.finish('invalid', learning_invalid=True)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(experiments.next_slot(self.service.current), planned)
        report = self.service.view()['experiment']['report']['arms'][planned['arm']]
        self.assertEqual(report['completed'], 0); self.assertEqual(report['invalid'], 1)
        self.service.prepare('retry'); self.finish('retry')
        self.assertEqual(experiments.next_slot(self.service.current)['index'], 1)

    def test_restart_interrupts_active_run_and_requires_manual_next(self):
        self.create(); self.service.prepare('interrupted')
        restarted = experiments.ExperimentService(ROOT, self.runs, lambda: self.store)
        restarted.recover()
        self.assertIsNone(restarted.current['active_run'])
        self.assertEqual(restarted.current['scored_training'], 0)
        self.assertFalse(restarted.current['runs'][0]['valid'])
        self.assertEqual(len(restarted.current['runs']), 1)
        restarted.recover()
        self.assertEqual(len(restarted.current['runs']), 1)

    def test_lost_registration_reply_does_not_start_or_leave_selected_match(self):
        self.create(); self.store.lose_reply = True
        with self.assertRaises(learning.LearningError): self.service.prepare('lost')
        self.assertTrue(self.service.pending())
        with self.assertRaises(learning.LearningError): self.service.prepare('second')
        self.service.retry()
        self.assertFalse(self.service.pending())
        self.assertIsNone(self.service.current['active_run'])
        self.assertFalse(self.service.current['runs'][0]['valid'])

    def test_finalization_lost_reply_replayed_once_before_restart_recovery(self):
        self.create(); self.service.prepare('lost-result'); self.store.lose_reply = True
        with self.assertRaises(learning.LearningError): self.finish('lost-result')
        restarted = experiments.ExperimentService(ROOT, self.runs, lambda: self.store)
        restarted.recover(); restarted.recover()
        self.assertEqual(restarted.current['scored_training'], 1)
        self.assertEqual(len(restarted.current['runs']), 1)
        self.assertTrue(restarted.current['runs'][0]['valid'])
        artifact = json.loads((self.runs / 'lost-result.learning.json').read_text())
        self.assertEqual(artifact['status'], 'scored')

    def test_unavailable_storage_blocks_freeze_and_preserves_intent(self):
        self.create(); self.train(); self.store.offline = True
        with self.assertRaises(learning.LearningError): self.service.freeze()
        self.assertEqual(self.service.current['phase'], 'training')
        with self.assertRaises(learning.LearningError): self.service.prepare('blocked')
        self.store.offline = False; self.service.retry()
        self.assertEqual(self.service.current['phase'], 'evaluation')

    def test_freeze_requires_valid_training_and_no_active_match(self):
        self.create()
        with self.assertRaises(ValueError): self.service.freeze()
        self.service.prepare('active')
        for action in (self.service.freeze, self.service.end, lambda: self.service.prepare('another')):
            with self.assertRaises(ValueError): action()
        self.finish('active', outcome='canceled')
        with self.assertRaises(ValueError): self.service.freeze()
        self.train('valid')
        with patch.object(experiments, 'scope_for', return_value='changed'):
            with self.assertRaises(ValueError): self.service.freeze()
        self.service.freeze()
        with self.assertRaises(ValueError): self.service.freeze()

    def test_import_compatible_seed_preserves_source_and_incompatible_rejected(self):
        settings = experiments.experiment_config(config())
        selected = learning.choose(settings, [], random.Random(3))
        row = {'id': 'seed-match', 'payload': {'decisions': selected['decisions']}, 'rewards': {'agent-1': 1, 'agent-2': .1}}
        checkpoint = {**learning.policy_state([row]), 'source_rows': [row]}
        self.store.catalog = [{'id': 1, 'algorithm': learning.ALGORITHM, 'catalog_version': learning.CATALOG,
            'contexts': [d['context'] for d in selected['decisions']], 'objectives': [learning.digest(settings['prompt'])]}]
        self.store.checkpoints[1] = {'state': checkpoint, 'scope': 'old-environment'}
        original = copy.deepcopy(self.store.checkpoints)
        self.create(checkpoint_id=1)
        self.assertEqual(self.service.current['scored_training'], 0)
        self.assertEqual(self.service.current['seed']['source_rows'], [row])
        self.train(); self.service.freeze()
        self.assertEqual(self.service.current['frozen']['state']['source_matches'], ['train', 'seed-match'])
        self.assertEqual(self.store.checkpoints, original)
        self.service.end()
        self.store.catalog[0]['contexts'][0]['model']['model'] = 'wrong-model'
        with self.assertRaises(ValueError): self.create(checkpoint_id=1)

    def test_timing_validation_and_report(self):
        self.create()
        for index, ended in enumerate((None, 99, float('nan'), float('inf'), True)):
            self.service.prepare('bad-' + str(index))
            self.assertEqual(self.finish('bad-' + str(index), ended_at=ended)['status'], 'skipped')
        self.train(); self.service.freeze()
        for i in range(8):
            self.service.prepare('eval-' + str(i))
            self.finish('eval-' + str(i), outcome='draw' if i % 2 else 'winner')
        for arm in self.service.view()['experiment']['report']['arms'].values():
            self.assertEqual(arm['completed'], 4)
            self.assertEqual(arm['win_rate'], arm['wins']/4)
            self.assertEqual(arm['median_victory_seconds'], 12.25 if arm['wins'] else None)
            self.assertLessEqual(arm['win_rate_interval'][0], arm['win_rate'])
            self.assertGreaterEqual(arm['win_rate_interval'][1], arm['win_rate'])

    def test_load_and_isolation_of_new_experiment(self):
        self.create(); self.train(); identity = self.service.current['id']; self.service.end()
        restarted = experiments.ExperimentService(ROOT, self.runs / 'other-machine', lambda: self.store)
        self.assertEqual(restarted.load(identity)['experiment']['scored_training'], 1)
        restarted.create(config(), 'agent-1')
        self.assertEqual(restarted.current['history'], [])
        self.assertEqual(self.store.rows[identity]['payload']['scored_training'], 1)


class ConfigTests(unittest.TestCase):
    def test_learning_guard_and_experiment_guard(self):
        ordinary = default_config()
        validate_config(ordinary)  # Existing mixed-model presets still work without learning.
        with self.assertRaises(ValueError): validate_config({**ordinary, 'learning_enabled': True})
        with self.assertRaises(ValueError): experiments.experiment_config(ordinary)
        value = config(); value['players'][0]['model'] = ''
        with self.assertRaises(ValueError): experiments.experiment_config(value)
        value = config(); value['players'].append({**value['players'][0], 'name': 'Third'})
        with self.assertRaises(ValueError): experiments.experiment_config(value)

    def test_compatible_endpoints_are_canonicalized(self):
        value = config()
        for p, url in zip(value['players'], ('https://EXAMPLE.com:443/v1/', 'https://example.com/v1')):
            p.update(harness='compatible', base_url=url)
        canonical = experiments.experiment_config(value)
        self.assertEqual(canonical['players'][0]['base_url'], canonical['players'][1]['base_url'])
        value['players'][1]['base_url'] = 'https://different.example/v1'
        with self.assertRaises(ValueError): experiments.experiment_config(value)

    def test_schedule_balances_each_four_run_block(self):
        for seed in range(20):
            slots = experiments.schedule(random.Random(seed))
            for block in (slots[:4], slots[4:]):
                self.assertEqual({(r['arm'], r['slot']) for r in block}, {(a,s) for a in ('baseline','learned') for s in (0,1)})


class DashboardExperimentTests(unittest.TestCase):
    setUp = ExperimentTests.setUp
    tearDown = ExperimentTests.tearDown

    def test_launch_failure_finalizes_interrupted_registration(self):
        import dashboard
        self.service.create(config(), 'agent-1')
        state = {**copy.deepcopy(dashboard.STATE), 'phase': 'idle'}
        with patch.object(dashboard, 'EXPERIMENTS', self.service), patch.object(dashboard, 'STATE', state), \
             patch.object(dashboard, 'RUNS', self.runs), patch.object(dashboard, 'check_credentials'), \
             patch.object(dashboard, 'save'), patch.object(dashboard.threading, 'Thread', side_effect=RuntimeError('Launch failed')):
            with self.assertRaises(RuntimeError): dashboard.start_match(config(), experiment=True)
        self.assertIsNone(self.service.current['active_run'])
        self.assertFalse(self.service.current['runs'][0]['valid'])
        self.assertEqual(state['phase'], 'error')

    def test_experiment_api_requires_operator_token_and_blocks_active_match(self):
        import dashboard
        from unittest.mock import Mock
        handler = dashboard.Handler.__new__(dashboard.Handler)
        handler.path = '/api/experiments/create'; handler.headers = {}
        handler.send = Mock(); handler.experiment_action = Mock()
        handler.do_POST()
        self.assertEqual(handler.send.call_args.args[0], 403)
        handler.experiment_action.assert_not_called()
        handler.headers = {'X-Arena-Control': dashboard.CONTROL}
        handler.do_POST(); handler.experiment_action.assert_called_once()
        del handler.experiment_action
        handler.json_body = Mock(return_value={'config':config(), 'learner':'agent-1'})
        with patch.object(dashboard, 'STATE', {'phase':'running'}), patch.object(dashboard, 'EXPERIMENTS', self.service):
            handler.experiment_action()
            self.assertEqual(handler.send.call_args.args[0], 400)
            self.assertIsNone(self.service.current)
        with patch.object(dashboard, 'STATE', {'phase':'idle'}), patch.object(dashboard, 'EXPERIMENTS', self.service):
            handler.experiment_action()
            self.assertEqual(handler.send.call_args.args[0], 200)
            self.assertEqual(self.service.current['phase'], 'training')

    def test_experiment_referee_finalizes_without_opening_or_settling_market(self):
        import dashboard
        from contextlib import ExitStack
        from unittest.mock import Mock, MagicMock
        self.service.create(config(), 'agent-1')
        settings, selected, info = self.service.prepare('referee-fixture')
        state = {**copy.deepcopy(dashboard.STATE), 'match_id':'referee-fixture', 'failure':False,
                 'learning_invalid':False, 'players':{p['id']:{**p,'alive':False} for p in settings['players']}}
        def command(*args, **kwargs):
            if 'inspect' in args:
                return Mock(stdout=json.dumps([{'NetworkSettings':{'Ports':{'8080/tcp':[{'HostPort':'1234'}]}}}]), returncode=0)
            return Mock(stdout='fixture-container', returncode=0)
        def apply(observations):
            state['players']['agent-2']['alive'] = False
            return 'Learner wins'
        thread = MagicMock(); thread.is_alive.return_value = False
        market = Mock()
        pool = MagicMock(); pool.__enter__.return_value.map.side_effect = lambda function, values: map(function, values)
        with ExitStack() as stack:
            replacements = {'STATE':state, 'RUNS':self.runs, 'EXPERIMENTS':self.service, 'MARKET':market,
                'command':command, 'process_table':Mock(return_value=[{'pid':'7','parent':'1','args':'python3 -u /opt/arena/supervisor.py'}, {'pid':'14','parent':'7','args':'python3 /opt/arena/gate.py'}]),
                'apply_observations':apply, 'observe':Mock(), 'refresh_eliminations':Mock(),
                'MatchObserver':MagicMock(), 'ThreadPoolExecutor':Mock(return_value=pool), 'save':Mock(), 'event':Mock(), 'finalize_forecast':Mock(),
                'settle_market':Mock(), 'settle_kill_markets':Mock(), 'FIRST_BLOOD':Mock(), 'FIRST_FALLEN':Mock(), 'STOP':Mock(is_set=Mock(return_value=False), wait=Mock(return_value=False))}
            for key, value in replacements.items(): stack.enter_context(patch.object(dashboard, key, value))
            stack.enter_context(patch.object(dashboard.threading, 'Thread', return_value=thread))
            # Fixture elapsed time advances beyond the scheduled common start.
            stack.enter_context(patch.object(dashboard.time, 'time', side_effect=[100, 103, 117]))
            dashboard.match(settings, selected, info)
            market.open.assert_not_called(); dashboard.FIRST_BLOOD.open.assert_not_called(); dashboard.FIRST_FALLEN.open.assert_not_called()
            dashboard.settle_market.assert_not_called(); dashboard.settle_kill_markets.assert_not_called()
        self.assertEqual(self.service.current['scored_training'], 1)
        self.assertEqual(self.service.current['runs'][0]['elapsed_seconds'], 15)
        self.assertEqual(state['phase'], 'finished')


if __name__ == '__main__': unittest.main()
