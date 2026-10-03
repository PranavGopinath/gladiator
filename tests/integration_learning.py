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
        print('Postgres checks passed: migration, service-role access, atomic finalization, duplicate retry, checkpoint, skipped episode, browser denial.')
    finally:
        run('docker', 'rm', '-f', '-v', name)


if __name__ == '__main__': main()
