"""Host-only contextual bandit. No network or credential reads on import.

Decisions are frozen before launch. Outcomes, not Jev estimates, train the policy.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shlex
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ALGORITHM = 'epsilon-greedy-context-v1'
EPSILON = .2
PRIOR_COUNT = 5
WINDOW = 200
STRATEGIES = {
    'baseline': {'label': 'Baseline', 'instruction': ''},
    'defense_first': {'label': 'Defense first', 'instruction':
        'Prioritize checking and protecting your own session and service availability before other moves. '
        'Verify the effect of changes and adapt when new evidence warrants a different priority.'},
    'investigation_first': {'label': 'Investigation first', 'instruction':
        'Prioritize a focused investigation of the environment before choosing a move. '
        'Use each observation to resolve a specific uncertainty; avoid repeating unchanged checks.'},
    'verification_first': {'label': 'Verification first', 'instruction':
        'Prioritize verifying observable effects after each consequential action. '
        'Distinguish an attempted action from a confirmed result and revise your plan when verification fails.'},
    'attack_first': {'label': 'Attack first', 'instruction':
        'Prioritize attacking the opponent\'s session and service availability before other moves. '
        'Verify the effect of changes and adapt when new evidence warrants a different priority.'},
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


CATALOG = digest(STRATEGIES)


class LearningError(RuntimeError):
    """Only fixed, non-sensitive error messages may cross this boundary."""


def scope_for(settings, root):
    files = ('Dockerfile', 'compose.yaml', 'arena_config.py', 'supervisor.py', 'session.py',
             'universal_harness.py', 'gate.py', 'entrypoint.sh', 'sshd_config')
    versions = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}
    return digest({'environment': versions, 'prompt': settings['prompt'], 'catalog': CATALOG,
                   'algorithm': ALGORITHM})


def context_for(settings, player):
    def model(p):
        return {'harness': p['harness'], 'model': p.get('model') or '<cli-default>',
                # Distinguish equal model IDs served by different compatible endpoints.
                'endpoint_id': digest(p['base_url']) if p.get('base_url') else None}
    return {'model': model(player), 'slot': player['id'],
            'opponents': sorted([model(p) for p in settings['players'] if p['id'] != player['id']],
                                key=lambda p: json.dumps(p, sort_keys=True)),
            'duration_seconds': settings['duration_seconds'],
            'turn_interval_seconds': settings['turn_interval_seconds']}


def policy_state(history):
    """Rebuildable state, bounded by whole matches, not individual decisions."""
    pooled, contexts = {}, {}
    for row in history[:WINDOW]:
        for decision in row['payload']['decisions']:
            if decision.get('learnable') is False:
                continue
            reward = row['rewards'].get(decision['player_id'])
            if reward is None:
                continue
            strategy = decision['strategy_id']
            key = digest(decision['context'])
            for target in (pooled, contexts.setdefault(key, {})):
                entry = target.setdefault(strategy, {'count': 0, 'total': 0.0})
                entry['count'] += 1
                entry['total'] += reward
    return {'algorithm': ALGORITHM, 'epsilon': EPSILON, 'prior_count': PRIOR_COUNT,
            'catalog_version': CATALOG, 'source_matches': [r['id'] for r in history[:WINDOW]],
            'pooled': pooled, 'contexts': contexts}


def distribution(state, context):
    scores = {}
    prior_count = state.get('prior_count', PRIOR_COUNT)
    epsilon = state.get('epsilon', EPSILON)
    for strategy in STRATEGIES:
        shared = state['pooled'].get(strategy, {'count': 0, 'total': 0})
        prior = shared['total'] / shared['count'] if shared['count'] else .5
        specific = state['contexts'].get(digest(context), {}).get(strategy, {'count': 0, 'total': 0})
        scores[strategy] = (specific['total'] + prior_count * prior) / (specific['count'] + prior_count)
    best = [s for s, score in scores.items() if math.isclose(score, max(scores.values()), abs_tol=1e-12)]
    return {s: epsilon / len(STRATEGIES) + ((1 - epsilon) / len(best) if s in best else 0)
            for s in STRATEGIES}, scores


def choose(settings, history, rng=None):
    rng = rng or random.SystemRandom()
    state = policy_state(history)
    version = digest(state)
    decisions = []
    for player in settings['players']:
        context = context_for(settings, player)
        probabilities, scores = distribution(state, context)
        selected = rng.choices(list(probabilities), weights=list(probabilities.values()), k=1)[0]
        decisions.append({'player_id': player['id'], 'context': context, 'strategy_id': selected,
                          'label': STRATEGIES[selected]['label'], 'instruction': STRATEGIES[selected]['instruction'],
                          'distribution': probabilities, 'selection_probability': probabilities[selected],
                          'estimated_rewards': scores, 'controller_version': version})
    return {'status': 'selected', 'controller_version': version, 'scored_matches': len(history[:WINDOW]),
            'catalog_version': CATALOG, 'decisions': decisions}


def outcome(snapshot):
    if snapshot.get('failure') or snapshot.get('outcome') not in ('winner', 'draw'):
        return {}, 'Match canceled, interrupted, or infrastructure-invalid'
    if snapshot.get('learning_invalid'):
        return {}, 'Provider, policy, or observation failure during the match'
    if any(p.get('error_category') and p['error_category'] != 'model_signal'
           for p in snapshot['players'].values()):
        return {}, 'Contestant infrastructure or provider failure'
    survivors = [p for p, info in snapshot['players'].items() if info.get('alive')]
    if snapshot['outcome'] == 'winner' and (len(survivors) != 1 or snapshot.get('winner') != survivors[0]):
        return {}, 'Inconsistent referee outcome'
    return {p: 1 / len(survivors) if p in survivors else .1 for p in snapshot['players']}, None


def runtime_settings(env_path):
    """Read only required settings at runtime. Never export or log their values."""
    names = ('SUPABASE_URL', 'SUPABASE_SECRET_KEY')
    values = {name: os.environ.get(name, '') for name in names}
    if not all(values.values()):
        try:
            with Path(env_path).open() as stream:
                for line in stream:
                    name, sep, raw = line.strip().removeprefix('export ').partition('=')
                    if sep and name.strip() in values and not values[name.strip()]:
                        parts = shlex.split(raw, comments=True)
                        values[name.strip()] = parts[0] if len(parts) == 1 else ''
        except (OSError, ValueError):
            raise LearningError('Cannot load Supabase runtime settings') from None
    if not all(values.values()):
        raise LearningError('Set SUPABASE_URL and SUPABASE_SECRET_KEY on the backend')
    return values


class SupabaseStore:
    def __init__(self, url, secret, opener=None):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment or '\n' in secret or '\r' in secret):
            raise LearningError('Invalid Supabase runtime settings')
        self._url, self._secret = url.rstrip('/') + '/rest/v1/', secret
        self._open = opener or urllib.request.urlopen

    @classmethod
    def from_environment(cls, path):
        values = runtime_settings(path)
        return cls(values['SUPABASE_URL'], values['SUPABASE_SECRET_KEY'])

    def request(self, route, body=None):
        headers = {'apikey': self._secret, 'Content-Type': 'application/json'}
        # New sb_secret keys are not JWTs; the API gateway handles their role.
        if not self._secret.startswith('sb_secret_'):
            headers['Authorization'] = 'Bearer ' + self._secret
        request = urllib.request.Request(self._url + route,
                    data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with self._open(request, timeout=8) as response:
                data = response.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise LearningError('Learning response exceeded the size limit')
            return json.loads(data)
        except urllib.error.HTTPError as error:
            error.close()
            raise LearningError('Supabase learning request failed; check credentials and apply the learning migration') from None
        except (OSError, ValueError):
            raise LearningError('Supabase learning storage is unavailable') from None

    def history(self, scope):
        query = urllib.parse.urlencode({'scope': 'eq.' + scope, 'status': 'eq.scored',
                   'order': 'finished_at.desc,id.desc', 'limit': WINDOW,
                   'select': 'id,payload,rewards'})
        rows = self.request('learning_matches?' + query)
        if not isinstance(rows, list):
            raise LearningError('Invalid learning history response')
        return rows

    def begin(self, record):
        self.request('rpc/learning_begin', {'record': record, 'catalog': STRATEGIES})

    def finish(self, record):
        # RPC builds the checkpoint from committed outcomes under the same transaction.
        return self.request('rpc/learning_finish', {'p_result': record})


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix('.tmp')
    with temp.open('w') as output:
        os.chmod(temp, 0o600)
        json.dump(value, output)
    temp.replace(path)


class LearningService:
    def __init__(self, root, runs, store_factory=None):
        self.root, self.runs = Path(root), Path(runs)
        self.store_factory = store_factory or (lambda: SupabaseStore.from_environment(self.root / '.env'))
        self.lock = threading.RLock()
        self.status = {'pending': 0, 'message': None}

    def prepare(self, identity, settings):
        if not settings.get('learning_enabled'):
            return None
        with self.lock:
            # No new decisions on an older checkpoint while outcomes await synchronization.
            self.retry()
            if self.pending() or list((self.runs / 'learning-registrations').glob('*.json')):
                raise LearningError('Learning outcomes await synchronization; retry or turn learning off')
            store = self.store_factory()
            scope = scope_for(settings, self.root)
            history = store.history(scope)
            learning = choose(settings, history)
            learning['scope'] = scope
            record = {'id': identity, 'scope': scope, 'catalog_version': CATALOG,
                      'controller_version': learning['controller_version'],
                      'payload': {'decisions': learning['decisions'], 'configuration': {
                          'duration_seconds': settings['duration_seconds'],
                          'turn_interval_seconds': settings['turn_interval_seconds'],
                          'objective_hash': digest(settings['prompt']),
                          'artifact_prefix': identity}, 'scored_matches': learning['scored_matches']}}
            # Write registration intent first so a lost HTTP response can be recovered.
            atomic_json(self.runs / 'learning-registrations' / (identity + '.json'), record)
            store.begin(record)
            atomic_json(self.runs / (identity + '.learning.json'), {**learning, 'match_id': identity})
            (self.runs / 'learning-registrations' / (identity + '.json')).unlink()
            return learning

    def pending(self):
        return sorted((self.runs / 'learning-outbox').glob('*.json'))

    def finalize(self, snapshot):
        learning = snapshot.get('learning')
        if not learning:
            return None
        rewards, reason = outcome(snapshot)
        record = {'id': snapshot['match_id'], 'rewards': rewards, 'reason': reason,
                  'outcome': snapshot.get('outcome'), 'started_at': snapshot.get('started_at'),
                  'ended_at': snapshot.get('ended_at')}
        with self.lock:
            # Durable local write precedes the network operation.
            atomic_json(self.runs / 'learning-outbox' / (record['id'] + '.json'), record)
            result = {**learning, 'status': 'pending', 'rewards': rewards, 'skip_reason': reason}
            atomic_json(self.runs / (record['id'] + '.learning.json'), result)
            self.retry()
            return json.loads((self.runs / (record['id'] + '.learning.json')).read_text())

    def retry(self):
        with self.lock:
            registrations = sorted((self.runs / 'learning-registrations').glob('*.json'))
            paths = self.pending()
            self.status = {'pending': len(paths) + len(registrations), 'message': None}
            if not paths and not registrations:
                return
            try:
                store = self.store_factory()
                for registration in registrations:
                    intent = json.loads(registration.read_text())
                    store.begin(intent)
                    store.finish({'id': intent['id'], 'rewards': {}, 'reason': 'Launch registration interrupted'})
                    registration.unlink()
                for path in paths:
                    record = json.loads(path.read_text())
                    response = store.finish(record)
                    artifact = self.runs / (record['id'] + '.learning.json')
                    learning = json.loads(artifact.read_text()) if artifact.exists() else {}
                    learning.update(status=response['status'], rewards=record['rewards'],
                                    skip_reason=record['reason'], next_controller_version=response.get('checkpoint_id'))
                    atomic_json(artifact, learning)
                    path.unlink()
                self.status = {'pending': 0, 'message': None}
            except Exception:
                self.status = {'pending': len(self.pending()) + len(list((self.runs / 'learning-registrations').glob('*.json'))), 'message': 'Learning sync pending; automatic retry scheduled'}

    def recover_interrupted(self, paths=None):
        # Called once on dashboard startup: never score a match whose referee died.
        for path in (list(self.runs.glob('*.learning.json')) if paths is None else paths):
            try:
                saved = json.loads(path.read_text())
                if saved.get('experiment_id'):
                    continue
                if (self.runs / 'learning-outbox' / path.name.replace('.learning.json', '.json')).exists():
                    continue  # A durable outcome takes precedence over an older local snapshot.
                if saved.get('status') == 'selected':
                    self.finalize({'learning': saved, 'match_id': path.name.removesuffix('.learning.json'),
                                   'outcome': 'interrupted'})
            except (OSError, ValueError):
                self.status['message'] = 'A learning recording needs inspection'
