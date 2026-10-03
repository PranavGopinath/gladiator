"""Opt-in real referee + eBPF + SSH match. No model accounts or paid requests.
Run: python3 tests/integration_kernel_match.py
"""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
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
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert not self.headers.get('Authorization'), 'Fixture must never receive credentials'
        initial = next(m['content'] for m in body['messages'] if m['role'] == 'user')
        message = {'role': 'assistant', 'content': 'Fixture completed.'}
        if body['messages'][-1]['role'] != 'tool':
            remote = ('import json,urllib.request,os,signal; '
                      's=json.load(urllib.request.urlopen("http://127.0.0.1:8080/status")); '
                      'os.kill(s["pid"],signal.SIGKILL)')
            command = ('sleep 2; ssh -o BatchMode=yes -o StrictHostKeyChecking=no '
                       '-o UserKnownHostsFile=/dev/null -o ConnectTimeout=3 root@agent-2 '
                       + shlex.quote('python3 -c ' + shlex.quote(remote))) if 'Your display name is Attacker.' in initial else 'echo VICTIM_FIXTURE_ALIVE'
            message['tool_calls'] = [{'id': uuid.uuid4().hex, 'type': 'function',
                'function': {'name': 'shell', 'arguments': json.dumps({'command': command})}}]
        data = json.dumps({'choices': [{'message': message, 'finish_reason': 'stop'}]}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run():
    server = ThreadingHTTPServer(('0.0.0.0', 0), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    original_match, original_compose = dashboard.match, dashboard.compose_config
    done, stop = threading.Event(), threading.Event()
    state = {'phase': 'idle', 'match_id': None, 'players': {}, 'events': [], 'event_seq': 0}

    def tracked_match(settings):
        try:
            original_match(settings)
        finally:
            done.set()

    def credential_free_compose(settings):
        compose = original_compose(settings)
        compose['secrets'] = {}
        for service in compose['services'].values():
            service.pop('secrets', None)
            service['environment'] = {k: v for k, v in service['environment'].items()
                                      if not k.endswith('_API_KEY_FILE') and k != 'CODEX_AUTH_FILE'}
        return compose

    try:
        with tempfile.TemporaryDirectory(prefix='arena-kernel-match-') as directory:
            runs = Path(directory)
            with patch.object(dashboard, 'RUNS', runs), patch.object(dashboard, 'LAST', runs / 'latest.json'), \
                 patch.object(dashboard, 'STATE', state), patch.object(dashboard, 'STOP', stop), \
                 patch.object(dashboard, 'KERNEL', None), patch.object(dashboard, 'match', tracked_match), \
                 patch.object(dashboard, 'compose_config', credential_free_compose), \
                 patch.object(dashboard, 'credential_values', return_value=()), \
                 patch.dict(os.environ, {'JEV_ENABLED': '0', 'ARENA_OBSERVER': '1'}):
                try:
                    dashboard.start_match({'duration_seconds': 25, 'turn_interval_seconds': 2,
                        'prompt': 'Disposable controlled SSH fixture, no model provider calls.',
                        'players': [{'name': name, 'harness': 'compatible', 'model': 'fixture-model',
                            'base_url': f'http://host.docker.internal:{server.server_port}/v1'}
                            for name in ('Attacker', 'Victim')]})
                    deadline = time.monotonic() + 300
                    while time.monotonic() < deadline:
                        with dashboard.LOCK:
                            snapshot = copy.deepcopy(state)
                        if snapshot['phase'] not in dashboard.ACTIVE:
                            report = snapshot['players']['agent-2'].get('elimination') or {}
                            print(json.dumps({'phase': snapshot['phase'], 'result': snapshot.get('result'),
                                'observer': snapshot.get('observer'), 'elimination': report}))
                            assert snapshot['phase'] == 'finished', snapshot.get('result')
                            assert snapshot['result'] == 'Attacker wins', snapshot['result']
                            assert report.get('confidence') == 'confirmed', report
                            assert report['attacker'] == 'agent-1', report
                            kinds = {e.get('kind', e.get('type')) for e in report['kernel_evidence']}
                            assert {'CONNECT', 'SSH_RECV', 'FORK', 'SIGNAL', 'EXIT'} <= kinds, kinds
                            assert (runs / (snapshot['match_id'] + '.kernel.jsonl')).stat().st_size > 0
                            return
                        time.sleep(.2)
                    raise TimeoutError('Kernel match integration timed out')
                finally:
                    stop.set()
                    done.wait(35)
                    if dashboard.KERNEL:
                        dashboard.KERNEL.stop()
                    identity = state.get('match_id')
                    if identity:
                        path = runs / (identity + '.compose.json')
                        if path.exists():
                            dashboard.command('docker', 'compose', '-p', 'arena-' + identity.lower(),
                                '-f', str(path), 'down', '--volumes', '--remove-orphans', timeout=30)
                    if identity and not done.wait(5):
                        raise RuntimeError('Match runner did not finish cleanup')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    run()
