"""Manual one-sided training and immutable, balanced controller evaluation.

All persistence is host-side. Imports and disabled experiments never load secrets.
"""
import copy
import json
import math
from pathlib import Path
import random
import statistics
import threading
import time
import uuid

from arena_config import validate_config, validate_learning_roster, model_identity
from arena_learning import (ALGORITHM, CATALOG, STRATEGIES, WINDOW, LearningError, SupabaseStore,
                            atomic_json, choose, context_for, digest, distribution, outcome,
                            policy_state, scope_for)


def experiment_config(value):
    settings = validate_config({**value, 'learning_enabled': False})
    validate_learning_roster(settings, experiment=True)
    # Canonical endpoint identity is also used in the policy's matchup features.
    for p in settings['players']:
        if p['harness'] == 'compatible':
            p['base_url'] = model_identity(p)[2]
    return settings


def ordered_config(experiment, slot):
    settings = copy.deepcopy(experiment['config'])
    original = settings['players']
    learner = next(p for p in original if p['id'] == experiment['learner'])
    opponent = next(p for p in original if p['id'] != experiment['learner'])
    settings['players'] = [learner, opponent] if slot == 0 else [opponent, learner]
    for index, player in enumerate(settings['players']):
        player['id'] = f'agent-{index + 1}'
    return settings


def schedule(rng=None):
    rng = rng or random.SystemRandom()
    result = []
    for _ in range(2):
        block = [{'arm': arm, 'slot': slot} for arm in ('baseline', 'learned') for slot in (0, 1)]
        rng.shuffle(block)
        result.extend(block)
    return result


def next_slot(experiment):
    if experiment['phase'] == 'training':
        attempts = sum(r['phase'] == 'training' for r in experiment['runs'])
        return {'arm': 'training', 'slot': attempts % 2, 'index': None}
    if experiment['phase'] == 'evaluation':
        index = sum(r['phase'] == 'evaluation' and r['valid'] for r in experiment['runs'])
        if index < len(experiment['schedule']):
            return {**experiment['schedule'][index], 'index': index}
    return None


def selected_policy(experiment, planned, rng=None):
    settings = ordered_config(experiment, planned['slot'])
    learner_id = f"agent-{planned['slot'] + 1}"
    if planned['arm'] == 'training':
        selected = choose(settings, experiment['history'], rng)
    else:
        frozen = experiment['frozen']
        state = frozen['state']
        selected = {'status': 'selected', 'catalog_version': frozen['catalog_version'],
                    'controller_version': frozen['version'], 'scored_matches': experiment['scored_training'],
                    'decisions': []}
        for player in settings['players']:
            context = context_for(settings, player)
            _, scores = distribution(state, context)
            best = max(scores.values())
            strategy = sorted(s for s, v in scores.items() if math.isclose(v, best, abs_tol=1e-12))[0]
            selected['decisions'].append({'player_id': player['id'], 'context': context,
                'strategy_id': strategy, **frozen['strategies'][strategy], 'estimated_rewards': scores,
                'distribution': {s: float(s == strategy) for s in frozen['strategies']},
                'selection_probability': 1.0, 'controller_version': frozen['version']})
    for decision in selected['decisions']:
        decision['learnable'] = planned['arm'] == 'training' and decision['player_id'] == learner_id
        decision['role'] = 'learner' if decision['player_id'] == learner_id else 'fixed opponent'
        if decision['player_id'] != learner_id or planned['arm'] == 'baseline':
            decision.update(strategy_id='baseline', **STRATEGIES['baseline'],
                            distribution={s: float(s == 'baseline') for s in STRATEGIES}, selection_probability=1.0)
    selected['evaluation_only'] = planned['arm'] != 'training'
    selected['experiment_id'] = experiment['id']
    selected['scope'] = 'experiment:' + experiment['id']
    selected['scored_matches'] = experiment['scored_training']
    return settings, selected, learner_id


def wilson(wins, total):
    if not total:
        return None
    z = 1.959963984540054
    p = wins / total
    denominator = 1 + z*z / total
    center = (p + z*z / (2*total)) / denominator
    half = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / denominator
    return [0 if wins == 0 else max(0, center-half), 1 if wins == total else min(1, center+half)]


def report(experiment):
    result = {'arms': {}, 'runs': experiment['runs'], 'pilot': True,
              'note': 'Eight valid matches are a pilot, not proof of improvement.'}
    for arm in ('baseline', 'learned'):
        attempts = [r for r in experiment['runs'] if r['phase'] == 'evaluation' and r['arm'] == arm]
        rows = [r for r in attempts if r['valid']]
        wins = [r for r in rows if r['outcome'] == 'win']
        result['arms'][arm] = {'completed': len(rows), 'target': 4, 'wins': len(wins),
            'losses': sum(r['outcome'] == 'loss' for r in rows), 'draws': sum(r['outcome'] == 'draw' for r in rows),
            'win_rate': len(wins)/len(rows) if rows else None, 'win_rate_interval': wilson(len(wins), len(rows)),
            'median_victory_seconds': statistics.median(r['elapsed_seconds'] for r in wins) if wins else None,
            'invalid': len(attempts)-len(rows)}
    return result


def checkpoint_reason(metadata, settings):
    if metadata.get('algorithm') != ALGORITHM or metadata.get('catalog_version') != CATALOG:
        return 'Different controller algorithm or strategy catalog'
    if not metadata.get('contexts') or metadata.get('objectives') != [digest(settings['prompt'])]:
        return 'Different task or missing task metadata'
    expected = context_for(settings, settings['players'][0]); expected.pop('slot')
    for context in metadata['contexts']:
        actual = dict(context); actual.pop('slot', None)
        if actual != expected:
            return 'Different model, opponent, endpoint, duration, or turn interval'
    return None


class ExperimentService:
    def __init__(self, root, runs, store_factory=None):
        self.root, self.runs = Path(root), Path(runs)
        self.directory = self.runs / 'experiments'
        self.outbox = self.runs / 'experiment-outbox'
        self.store_factory = store_factory or (lambda: SupabaseStore.from_environment(self.root / '.env'))
        self.lock = threading.RLock()
        self.current = None
        self.status = None
        pointer = self.directory / 'current.json'
        if pointer.exists():
            try:
                self.current = json.loads(pointer.read_text())
            except (OSError, ValueError):
                self.status = 'Experiment cache needs inspection'

    def pending(self):
        return list(self.outbox.glob('*.json'))

    def _apply(self, envelope):
        reply = self.store_factory().request('rpc/learning_experiment_commit', envelope['request'])
        payload = envelope['request']['p_payload']
        saved = {**payload, 'revision': reply['revision']}
        atomic_json(self.directory / (saved['id'] + '.json'), saved)
        atomic_json(self.directory / 'current.json', saved)
        self.current = saved
        run = envelope['request'].get('p_run')
        if run:
            artifact = {**run['learning'], 'status': run['status'], 'rewards': run.get('rewards', {}),
                        'skip_reason': run.get('reason'), 'evaluation_only': run['phase'] == 'evaluation'}
            atomic_json(self.runs / (run['id'] + '.learning.json'), artifact)
        (self.outbox / (saved['id'] + '.json')).unlink(missing_ok=True)
        self.status = None
        return saved

    def _commit(self, payload, run=None, registration=None, result=None, checkpoint=None, abandon=False):
        expected = payload.pop('revision', 0)
        envelope = {'abandon': abandon, 'request': {'p_id': payload['id'], 'p_expected_revision': expected,
            'p_payload': payload, 'p_run': run, 'p_registration': registration, 'p_result': result,
            'p_checkpoint': checkpoint}}
        atomic_json(self.outbox / (payload['id'] + '.json'), envelope)
        try:
            return self._apply(envelope)
        except Exception:
            self.status = 'Experiment synchronization pending; retry before starting another match'
            raise LearningError(self.status) from None

    def retry(self):
        with self.lock:
            for path in self.pending():
                try:
                    envelope = json.loads(path.read_text())
                    self._apply(envelope)
                    if envelope.get('abandon') and self.current.get('active_run'):
                        self.finalize({'outcome': 'interrupted', 'match_id': self.current['active_run']['id']})
                except Exception:
                    self.status = 'Experiment synchronization pending; automatic retry scheduled'
                    return

    def recover(self):
        with self.lock:
            self.retry()
            if not self.pending() and self.current and self.current.get('active_run'):
                self.finalize({'outcome': 'interrupted', 'match_id': self.current['active_run']['id']})

    def ready(self):
        if self.pending():
            raise LearningError('Experiment synchronization pending; wait for retry')
        if self.status and self.current is None:
            raise LearningError(self.status)

    def checkpoints(self, config):
        settings = experiment_config(config)
        rows = self.store_factory().request('rpc/learning_checkpoint_catalog', {})
        return [{**row, 'incompatible_reason': checkpoint_reason(row, settings)} for row in rows]

    def load(self, identity):
        with self.lock:
            self.ready()
            if self.current and self.current.get('active_run'):
                raise ValueError('Stop the current match before loading an experiment')
            if self.current and self.current['phase'] in ('training', 'evaluation') and self.current['id'] != identity:
                raise ValueError('End the current experiment before loading another')
            rows = self.store_factory().request('learning_experiments?id=eq.' + str(uuid.UUID(identity)) + '&select=payload,revision')
            if not rows:
                raise ValueError('Experiment not found')
            self.current = {**rows[0]['payload'], 'revision': rows[0]['revision']}
            atomic_json(self.directory / 'current.json', self.current)
            self.recover()
            return self.view()

    def create(self, config, learner, checkpoint_id=None):
        with self.lock:
            self.ready()
            if self.current and self.current['phase'] in ('training', 'evaluation'):
                raise ValueError('End the current experiment before creating another')
            settings = experiment_config(config)
            if learner not in {p['id'] for p in settings['players']}:
                raise ValueError('Choose a learner from the configured roster')
            history, seed = [], None
            if checkpoint_id is not None:
                if isinstance(checkpoint_id, bool) or not isinstance(checkpoint_id, int) or checkpoint_id < 1:
                    raise ValueError('Invalid checkpoint ID')
                catalog = self.checkpoints(settings)
                metadata = next((r for r in catalog if r['id'] == checkpoint_id), None)
                if not metadata or metadata['incompatible_reason']:
                    raise ValueError(metadata['incompatible_reason'] if metadata else 'Checkpoint not found')
                rows = self.store_factory().request(f'learning_checkpoints?id=eq.{checkpoint_id}&select=state,scope')
                state = rows[0]['state']
                history = copy.deepcopy(state['source_rows'][:WINDOW])
                seed = {'checkpoint_id': checkpoint_id, 'scope': rows[0]['scope'], 'state': policy_state(history), 'source_rows': copy.deepcopy(history)}
            manifest = {'id': str(uuid.uuid4()), 'phase': 'training', 'revision': 0,
                'config': settings, 'learner': learner, 'scope': scope_for(settings, self.root),
                'algorithm': ALGORITHM, 'catalog_version': CATALOG, 'strategies': copy.deepcopy(STRATEGIES),
                'created_at': time.time(), 'seed': seed, 'history': history, 'scored_training': 0,
                'runs': [], 'active_run': None, 'frozen': None, 'schedule': []}
            self._commit(manifest)
            return self.view()

    def assert_environment(self):
        if self.current['scope'] != scope_for(self.current['config'], self.root):
            raise ValueError('Environment or strategy definitions changed; create a new experiment')

    def freeze(self):
        with self.lock:
            self.ready()
            if not self.current or self.current['phase'] != 'training' or self.current['active_run']:
                raise ValueError('Freeze requires an idle training experiment')
            if self.current['scored_training'] < 1:
                raise ValueError('Complete at least one scored training match before freezing')
            self.assert_environment()
            manifest = copy.deepcopy(self.current)
            state = policy_state(manifest['history'])
            manifest.update(phase='evaluation', schedule=schedule(), frozen={
                'state': state, 'version': digest(state), 'catalog_version': CATALOG,
                'strategies': copy.deepcopy(manifest['strategies']), 'source_rows': copy.deepcopy(manifest['history']), 'frozen_at': time.time()})
            self._commit(manifest)
            return self.view()

    def end(self):
        with self.lock:
            self.ready()
            if not self.current or self.current['active_run']:
                raise ValueError('Stop the active match before ending the experiment')
            if self.current['phase'] not in ('training', 'evaluation'):
                raise ValueError('Experiment is already finished')
            manifest = copy.deepcopy(self.current)
            manifest['phase'] = 'ended'
            self._commit(manifest)
            return self.view()

    def prepare(self, match_id):
        with self.lock:
            self.ready()
            if not self.current or self.current['active_run']:
                raise ValueError('No idle experiment is ready')
            self.assert_environment()
            planned = next_slot(self.current)
            if not planned:
                raise ValueError('Experiment has no remaining matches')
            settings, learning, learner_id = selected_policy(self.current, planned)
            manifest = copy.deepcopy(self.current)
            run = {'id': match_id, 'experiment_id': manifest['id'], 'phase': manifest['phase'],
                **planned, 'learner_id': learner_id, 'status': 'selected', 'learning': learning,
                'controller_version': learning['controller_version'], 'created_at': time.time()}
            manifest['active_run'] = run
            registration = None
            if planned['arm'] == 'training':
                registration = {'id': match_id, 'scope': 'experiment:' + manifest['id'], 'catalog_version': CATALOG,
                    'controller_version': learning['controller_version'], 'payload': {
                        'decisions': learning['decisions'], 'configuration': {
                            'objective_hash': digest(settings['prompt']), 'duration_seconds': settings['duration_seconds'],
                            'turn_interval_seconds': settings['turn_interval_seconds'], 'artifact_prefix': match_id}}}
                run['registration'] = registration
            self._commit(manifest, run=run, registration=registration, abandon=True)
            return settings, learning, {k: run[k] for k in ('experiment_id', 'phase', 'arm', 'slot', 'index', 'learner_id')}

    def finalize(self, snapshot):
        with self.lock:
            self.ready()
            if not self.current or not self.current.get('active_run'):
                return
            if snapshot['match_id'] != self.current['active_run']['id']:
                raise ValueError('Experiment match identity mismatch')
            manifest = copy.deepcopy(self.current)
            run = manifest['active_run']
            rewards, reason = outcome(snapshot)
            elapsed = None
            if not reason:
                start, end = snapshot.get('started_at'), snapshot.get('ended_at')
                if (any(isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) for t in (start, end)) or end < start):
                    reason, rewards = 'Invalid referee timing', {}
                else:
                    elapsed = end-start
            learner_outcome = ('win' if snapshot.get('winner') == run['learner_id'] else
                               'draw' if snapshot.get('outcome') == 'draw' and snapshot.get('players', {}).get(run['learner_id'], {}).get('alive') else 'loss')
            # A simultaneous zero-survivor referee draw is also a draw in the report.
            if snapshot.get('outcome') == 'draw' and not any(p.get('alive') for p in snapshot.get('players', {}).values()):
                learner_outcome = 'draw'
            def timestamp(key):
                value = snapshot.get(key)
                return value if type(value) in (int, float) and math.isfinite(value) else None
            run.update(status='skipped' if reason else 'scored', valid=not bool(reason), reason=reason,
                       rewards=rewards, outcome=learner_outcome if not reason else None, elapsed_seconds=elapsed,
                       started_at=timestamp('started_at'), ended_at=timestamp('ended_at'))
            manifest['runs'].append({k: v for k, v in run.items() if k not in ('learning', 'registration')})
            manifest['active_run'] = None
            result, checkpoint = None, None
            if run['phase'] == 'training':
                result = {'id': run['id'], 'rewards': rewards, 'reason': reason, 'outcome': snapshot.get('outcome'),
                          'started_at': run['started_at'], 'ended_at': run['ended_at']}
                if not reason:
                    row = {**run['registration'], 'rewards': rewards}
                    manifest['history'] = ([row] + manifest['history'])[:WINDOW]
                    manifest['scored_training'] += 1
                    checkpoint = {**policy_state(manifest['history']), 'source_rows': manifest['history']}
            elif sum(r['phase'] == 'evaluation' and r['valid'] for r in manifest['runs']) == 8:
                manifest['phase'] = 'complete'
            self._commit(manifest, run=run, result=result, checkpoint=checkpoint)
            artifact = {**run['learning'], 'status': run['status'], 'rewards': rewards, 'skip_reason': reason,
                        'evaluation_only': run['phase'] == 'evaluation'}
            return artifact

    def view(self):
        with self.lock:
            if not self.current:
                return {'experiment': None, 'sync_message': self.status, 'pending': bool(self.pending())}
            manifest = self.current
            public = {k: copy.deepcopy(v) for k, v in manifest.items() if k not in ('history', 'seed', 'frozen', 'strategies', 'active_run', 'runs')}
            public['seed_checkpoint'] = manifest['seed']['checkpoint_id'] if manifest['seed'] else None
            public['frozen_version'] = manifest['frozen']['version'] if manifest['frozen'] else None
            public['active_run'] = ({k: v for k, v in manifest['active_run'].items() if k not in ('learning', 'registration')}
                                    if manifest['active_run'] else None)
            public['report'] = report(manifest)
            public['next'] = next_slot(manifest)
            if public['next']:
                settings, learning, learner_id = selected_policy(manifest, public['next']) if manifest['phase'] == 'evaluation' else (ordered_config(manifest, public['next']['slot']), None, f"agent-{public['next']['slot'] + 1}")
                public['next'].update(players=settings['players'], learner_id=learner_id,
                                     strategy=next(d['label'] for d in learning['decisions'] if d['player_id'] == learner_id) if learning else 'Sampled when started')
            return {'experiment': public, 'sync_message': self.status, 'pending': bool(self.pending())}
