"""Exercise the migration in disposable Postgres. No Supabase account or .env reads.
Run: python3 tests/integration_learning.py
Uses an existing pgvector/pgvector:pg16 image; override via LEARNING_TEST_POSTGRES_IMAGE.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tempfile
import copy
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from arena_config import validate_config
from arena_learning import STRATEGIES, CATALOG, choose


def run(*args, input=None):
    result = subprocess.run(args, input=input, text=True, capture_output=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def main():
    name = 'gladiator-learning-test-' + uuid.uuid4().hex[:10]
    image = os.environ.get('LEARNING_TEST_POSTGRES_IMAGE', 'pgvector/pgvector:pg16')
    run('docker', 'run', '-d', '--name', name, '--network', 'none',
        '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', image)
    def sql(statement):
        return run('docker', 'exec', '-i', name, 'psql', '-h', '127.0.0.1', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1', '-At', input=statement)
    def literal(value): return "'" + json.dumps(value).replace("'", "''") + "'::jsonb"
    try:
        for _ in range(100):
            if subprocess.run(['docker', 'exec', name, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres'], capture_output=True).returncode == 0:
                break
            time.sleep(.1)
        sql('create role anon; create role authenticated; create role service_role bypassrls;')
        sql((ROOT / 'supabase/migrations/202610040001_gladiator_learning.sql').read_text())
        selected = choose(validate_config({}), [])
        record = {'id': 'fixture', 'scope': 'fixture-scope', 'catalog_version': CATALOG,
                  'controller_version': selected['controller_version'], 'payload': {'decisions': selected['decisions']}}
        # Seed the original schema before migrating to prove history survives.
        sql('set role service_role; select public.gladiator_learning_begin(' + literal(record) + ',' + literal(STRATEGIES) + ');')
        rename = (ROOT / 'supabase/migrations/202610040002_learning_names.sql').read_text()
        sql(rename)
        sql(rename)  # Safe if manually reapplied.
        assert sql('select count(*) from public.learning_matches;') == '1'
        assert sql("select to_regclass('public.gladiator_learning_matches') is null;") == 't'

        sql('select public.learning_begin(' + literal(record) + ',' + literal(STRATEGIES) + ');')
        result = {'id': 'fixture', 'rewards': {'agent-1': 1, 'agent-2': .1}, 'reason': None}
        try:
            sql('select public.learning_finish(' + literal(dict(result, rewards={'agent-1': 1})) + ');')
            raise AssertionError('Incomplete rewards were accepted')
        except AssertionError as error:
            assert 'Reward roster' in str(error)
        assert sql("select status from public.learning_matches where id='fixture';") == 'selected'
        assert sql('select count(*) from public.learning_checkpoints;') == '0'
        first = sql('set role service_role; select public.learning_finish(' + literal(result) + ');').splitlines()[-1]
        again = sql('select public.learning_finish(' + literal(result) + ');')
        assert json.loads(first) == json.loads(again)
        assert sql('select count(*) from public.learning_checkpoints;') == '1'
        assert sql('select sum(reward) from public.learning_decisions;') == '1.1'
        checkpoint = json.loads(sql('select state from public.learning_checkpoints;'))
        assert checkpoint['source_matches'] == ['fixture']
        assert sum(v['count'] for v in checkpoint['pooled'].values()) == 2
        record['id'] = 'skipped'
        sql('select public.learning_begin(' + literal(record) + ',' + literal(STRATEGIES) + ');')
        skipped = {'id': 'skipped', 'rewards': {}, 'reason': 'Interrupted'}
        sql('select public.learning_finish(' + literal(skipped) + ');')
        assert sql('select count(*) from public.learning_checkpoints;') == '1'
        assert sql("select has_table_privilege('anon','public.learning_matches','SELECT');") == 'f'
        assert sql("select has_function_privilege('authenticated','public.learning_finish(jsonb)','EXECUTE');") == 'f'
        sql((ROOT / 'supabase/migrations/202610040003_learning_experiments.sql').read_text())
        check_experiments(sql, literal)
        print('Postgres checks passed: migration, service-role access, atomic finalization, duplicate retry, checkpoint, skipped episode, browser denial, one-sided training, frozen evaluation, experiment transaction rollback and retry.')
    finally:
        run('docker', 'rm', '-f', '-v', name)


def check_experiments(sql, literal):
    from arena_experiments import ExperimentService
    class PostgresStore:
        def request(self, route, body=None):
            if route == 'rpc/learning_experiment_commit':
                args = []
                for key, value in body.items():
                    if key == 'p_id': atom = "'" + str(uuid.UUID(value)) + "'::uuid"
                    elif key == 'p_expected_revision': atom = str(int(value)) + '::bigint'
                    else: atom = 'null' if value is None else literal(value)
                    args.append(key + ' => ' + atom)
                query = 'select public.learning_experiment_commit(' + ','.join(args) + ');'
            elif route == 'rpc/learning_checkpoint_catalog':
                query = 'select public.learning_checkpoint_catalog();'
            elif route.startswith('learning_checkpoints?'):
                identity = int(route.split('eq.')[1].split('&')[0])
                query = f"select jsonb_agg(jsonb_build_object('state',state,'scope',scope)) from public.learning_checkpoints where id={identity};"
            else:
                raise AssertionError(route)
            return json.loads(sql('set role service_role; ' + query).splitlines()[-1])
    def snapshot(service, identity):
        learner = service.current['active_run']['learner_id']
        return {'match_id': identity, 'outcome': 'winner', 'winner': learner,
                'started_at': 100, 'ended_at': 117,
                'players': {p: {'alive': p == learner} for p in ('agent-1','agent-2')}}
    with tempfile.TemporaryDirectory() as directory:
        settings = validate_config({})
        for player in settings['players']: player.update(harness='codex', model='fixture-model')
        store = PostgresStore()
        service = ExperimentService(ROOT, Path(directory), lambda: store)
        service.create(settings, 'agent-1')
        service.prepare('experiment-train')
        # Missing checkpoint must roll back both learning_finish and the manifest/run.
        before = copy.deepcopy(service.current)
        request = {'p_id': before['id'], 'p_expected_revision': before['revision'],
                   'p_payload': {k:v for k,v in before.items() if k != 'revision'},
                   'p_run': before['active_run'], 'p_result': {
                       'id':'experiment-train', 'rewards': {'agent-1':1,'agent-2':.1}, 'reason':None}}
        try:
            store.request('rpc/learning_experiment_commit', request)
            raise AssertionError('Incomplete transaction accepted')
        except AssertionError as error:
            assert 'Missing experiment checkpoint' in str(error)
        assert sql("select status from public.learning_matches where id='experiment-train';") == 'selected'
        assert sql("select count(*) from public.learning_checkpoints where match_id='experiment-train';") == '0'
        service.finalize(snapshot(service, 'experiment-train'))
        checkpoint = json.loads(sql("select state from public.learning_checkpoints where match_id='experiment-train';"))
        assert sum(v['count'] for v in checkpoint['pooled'].values()) == 1
        assert sum(v['total'] for v in checkpoint['pooled'].values()) == 1
        assert len([r for r in service.checkpoints(settings) if not r['incompatible_reason']]) == 1
        # Exercise the SQL learnable filter directly, without the Python checkpoint replacement.
        registration = copy.deepcopy(before['active_run']['registration'])
        registration['id'], registration['scope'] = 'one-sided-sql', 'separate-scope'
        sql('select public.learning_begin(' + literal(registration) + ',' + literal(STRATEGIES) + ');')
        sql('select public.learning_finish(' + literal({'id':'one-sided-sql','rewards':{'agent-1':1,'agent-2':.1},'reason':None}) + ');')
        pure_sql = json.loads(sql("select state from public.learning_checkpoints where match_id='one-sided-sql';"))
        assert sum(v['count'] for v in pure_sql['pooled'].values()) == 1
        service.freeze()
        frozen = copy.deepcopy(service.current['frozen'])
        count = sql('select count(*) from public.learning_checkpoints;')
        for index in range(8):
            identity = 'experiment-eval-' + str(index)
            service.prepare(identity)
            if index == 0:
                bad = copy.deepcopy(service.current); bad.pop('revision'); bad['frozen']['version'] = 'tampered'
                try:
                    store.request('rpc/learning_experiment_commit', {'p_id':bad['id'], 'p_expected_revision':service.current['revision'], 'p_payload':bad})
                    raise AssertionError('Frozen mutation accepted')
                except AssertionError as error:
                    assert 'Frozen controller is immutable' in str(error)
                try:
                    store.request('rpc/learning_experiment_commit', {'p_id':service.current['id'], 'p_expected_revision':service.current['revision'], 'p_payload':{k:v for k,v in service.current.items() if k!='revision'}, 'p_run':service.current['active_run'], 'p_result':{'id':identity}})
                    raise AssertionError('Evaluation trained')
                except AssertionError as error:
                    assert 'Evaluation cannot train' in str(error)
            service.finalize(snapshot(service, identity))
        assert service.current['phase'] == 'complete'
        assert service.current['frozen'] == frozen
        assert sql('select count(*) from public.learning_checkpoints;') == count
        assert sql("select count(*) from public.learning_matches where id like 'experiment-eval-%';") == '0'
        # Replay final transaction: same revision, no duplicate run, no extra checkpoint.
        request = {'p_id':service.current['id'], 'p_expected_revision':service.current['revision']-1,
                   'p_payload':{k:v for k,v in service.current.items() if k!='revision'}}
        assert store.request('rpc/learning_experiment_commit',request)['revision'] == service.current['revision']
        assert sql('select count(*) from public.learning_experiment_runs;') == '9'
        assert sql("select has_table_privilege('anon','public.learning_experiments','SELECT');") == 'f'
        assert sql("select has_function_privilege('authenticated','public.learning_checkpoint_catalog()','EXECUTE');") == 'f'
        assert sql("select has_function_privilege('anon','public.learning_experiment_commit(uuid,bigint,jsonb,jsonb,jsonb,jsonb,jsonb)','EXECUTE');") == 'f'
        # Imported checkpoint receives a separate history; no source row is overwritten.
        checkpoint_id = int(sql("select id from public.learning_checkpoints where match_id='experiment-train';"))
        service.create(settings, 'agent-2', checkpoint_id)
        assert service.current['scored_training'] == 0
        assert service.current['history'][0]['id'] == 'experiment-train'
        service.prepare('imported-training'); service.finalize(snapshot(service, 'imported-training'))
        assert service.current['history'][1]['id'] == 'experiment-train'
        assert json.loads(sql("select state from public.learning_checkpoints where match_id='experiment-train';")) == checkpoint


if __name__ == '__main__': main()
