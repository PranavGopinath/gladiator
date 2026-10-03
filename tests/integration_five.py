"""Opt-in Docker check with a local model fixture; never calls a paid provider.

Run directly: python3 tests/integration_five.py
"""
import argparse
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path != '/v1/chat/completions' or self.headers.get('Authorization'):
            self.send_error(400, 'Unexpected request or credential')
            return
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if body['model'] != 'fixture-model':
            self.send_error(400, 'Unexpected model')
            return
        identity = next(m['content'] for m in body['messages'] if m['role'] == 'user')
        with self.server.fixture_lock:
            fail = self.server.fail_first and identity not in self.server.seen
            self.server.seen.add(identity)
        message = {'role': 'assistant', 'content': 'Fixture observation complete.'}
        if body['messages'][-1]['role'] != 'tool':
            message = {'role': 'assistant', 'content': 'Fixture: observing this computer.',
                       'tool_calls': [{'id': 'call-' + uuid.uuid4().hex, 'type': 'function',
                                       'function': {'name': 'shell', 'arguments': json.dumps({
                                           'command': 'printf "ARENA_FIXTURE_OK\\n"; hostname'})}}]}
        payload = json.dumps({'choices': [{'message': None if fail else message, 'finish_reason': 'stop'}],
                              'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def run(timeout, turn_error=False):
    server = ThreadingHTTPServer(('0.0.0.0', 0), Fixture)
    server.fail_first, server.seen, server.fixture_lock = turn_error, set(), threading.Lock()
    server_thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
    server_thread.start()
    deadline = time.monotonic() + timeout
    done = threading.Event()
    stop = threading.Event()
    original_match, original_command = dashboard.match, dashboard.command
    original_compose = dashboard.compose_config
    state = {'phase': 'idle', 'match_id': None, 'players': {}, 'events': [], 'event_seq': 0}

    def tracked_match(settings):
        try:
            original_match(settings)
        finally:
            done.set()

    def bounded_command(*args, timeout=20, **kwargs):
        return original_command(*args, timeout=min(timeout, max(1, deadline - time.monotonic())), **kwargs)

    def credential_free_compose(settings):
        compose = original_compose(settings)
        compose['secrets'] = {}
        for service in compose['services'].values():
            service.pop('secrets', None)
            for key in list(service['environment']):
                if key.endswith('_API_KEY_FILE') or key == 'CODEX_AUTH_FILE':
                    del service['environment'][key]
        return compose

    try:
        with tempfile.TemporaryDirectory(prefix='gladiator-integration-') as directory:
            runs = Path(directory)
            with patch.object(dashboard, 'RUNS', runs), patch.object(dashboard, 'LAST', runs / 'latest.json'), \
                 patch.object(dashboard, 'STATE', state), patch.object(dashboard, 'STOP', stop), \
                 patch.object(dashboard, 'match', tracked_match), patch.object(dashboard, 'command', bounded_command), \
                 patch.object(dashboard, 'compose_config', credential_free_compose), \
                 patch.object(dashboard, 'credential_values', return_value=()), \
                 patch.dict(os.environ, {'JEV_ENABLED': '0'}):
                killed = False
                try:
                    dashboard.start_match({'duration_seconds': 60, 'turn_interval_seconds': 2,
                                           'prompt': 'Local integration fixture. Observe your computer with the shell tool.',
                                           'players': [{'name': f'Fixture {index}', 'harness': 'compatible',
                                                        'model': 'fixture-model',
                                                        'base_url': f'http://host.docker.internal:{server.server_port}/v1'}
                                                       for index in range(1, 6)]})
                    while time.monotonic() < deadline:
                        with dashboard.LOCK:
                            snapshot = copy.deepcopy(state)
                        completed = {event['player'] for event in snapshot['events']
                                     if event['kind'] == 'tool' and event['data'].get('status') == 'completed'
                                     and 'ARENA_FIXTURE_OK' in (event['data'].get('output') or '')}
                        if snapshot['phase'] == 'running' and not killed and len(completed) == 5:
                            assert len(snapshot['players']) == 5, snapshot
                            if turn_error:
                                recovered = {e['player'] for e in snapshot['events']
                                             if e['kind'] == 'session' and e['data'].get('phase') == 'turn_error'}
                                assert recovered == completed, recovered
                                assert not any(e['kind'] == 'elimination' for e in snapshot['events'])
                            for identity in ('agent-1', 'agent-2', 'agent-3', 'agent-4'):
                                bounded_command('docker', 'kill', snapshot['players'][identity]['container_id'])
                            killed = True
                        if snapshot['phase'] not in dashboard.ACTIVE:
                            assert killed and snapshot['phase'] == 'finished', snapshot.get('result')
                            assert snapshot['result'] == 'Fixture 5 wins', snapshot['result']
                            alive = [identity for identity, player in snapshot['players'].items() if player['alive']]
                            assert alive == ['agent-5'], alive
                            assert sum(event['kind'] == 'elimination' for event in snapshot['events']) == 4
                            print(json.dumps({'phase': snapshot['phase'], 'result': snapshot['result'],
                                              'contestants': 5, 'shell_verified': sorted(completed),
                                              'provider_error_recovery_verified': turn_error,
                                              'eliminated': 4, 'alive': alive}))
                            return
                        time.sleep(.2)
                    raise TimeoutError(f'Five-contestant integration check exceeded {timeout:g} seconds')
                finally:
                    # Keep the temporary files and patched globals available until
                    # the runner completes its own cleanup. Remove this project only.
                    stop.set()
                    done.wait(35)
                    match_id = state.get('match_id')
                    if match_id:
                        compose_file = runs / (match_id + '.compose.json')
                        if compose_file.exists():
                            cleanup = original_command('docker', 'compose', '--project-name', 'arena-' + match_id.lower(),
                                                       '-f', str(compose_file), 'down', '--volumes', '--remove-orphans',
                                                       timeout=30, check=False)
                            if cleanup.returncode:
                                raise RuntimeError('Integration project cleanup failed: ' + cleanup.stderr.strip())
                    if match_id and not done.wait(5):
                        raise RuntimeError('Integration runner did not finish during bounded cleanup')
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, default=240, help='Startup/match timeout in seconds (30–600; default 240)')
    parser.add_argument('--turn-error', action='store_true', help='Return a malformed first response to every contestant and verify recovery')
    arguments = parser.parse_args()
    if not 30 <= arguments.timeout <= 600:
        parser.error('--timeout must be between 30 and 600 seconds')
    try:
        run(arguments.timeout, arguments.turn_error)
    except (AssertionError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit(f'Five-contestant integration check failed: {exc}') from exc
