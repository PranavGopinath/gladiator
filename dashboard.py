"""Local spectator UI and external process referee. python3 dashboard.py"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / '.runs'
RUNS.mkdir(exist_ok=True, mode=0o700)
CONTROL = secrets.token_urlsafe(24)
LOCK = threading.RLock()
STOP = threading.Event()
STATE = {'phase': 'idle', 'result': None, 'started_at': None, 'ended_at': None,
         'limit': 300, 'match_id': None, 'players': {}, 'events': []}
LAST = RUNS / 'latest.json'
if LAST.exists():
    STATE.update(json.loads(LAST.read_text()))
    if STATE['phase'] in ('preparing', 'running'):
        STATE.update(phase='interrupted', result='Dashboard restarted; previous match is not being refereed.')


def command(*args, timeout=20, check=True, input=None):
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                          timeout=timeout, check=check, input=input)


def clean(text):
    text = re.sub(r'sk-[A-Za-z0-9_-]+|eyJ[A-Za-z0-9_.-]{30,}', '[credential redacted]', str(text))
    return re.sub(r'(?i)(Bearer\s+)\S+', r'\1[redacted]', text)[:14000]


def event(player, kind, text):
    with LOCK:
        row = {'time': time.time(), 'player': player, 'kind': kind, 'text': clean(text)}
        STATE['events'].append(row)
        STATE['events'] = STATE['events'][-1500:]
        match_id = STATE['match_id']
        if match_id:
            with (RUNS / (match_id + '.jsonl')).open('a') as output:
                output.write(json.dumps(row) + '\n')


def save():
    with LOCK:
        temp = LAST.with_suffix('.tmp')
        temp.write_text(json.dumps(STATE))
        temp.replace(LAST)


def process_table(cid):
    result = command('docker', 'top', cid, '-eo', 'pid,ppid,stat,args', check=False)
    if result.returncode:
        return None
    rows = []
    for line in result.stdout.splitlines()[1:]:
        fields = line.split(None, 3)
        if len(fields) == 4:
            rows.append({'pid': fields[0], 'parent': fields[1], 'status': fields[2], 'args': fields[3]})
    return rows


def parse_log(player, line):
    try:
        data = json.loads(line)
    except ValueError:
        if line.strip():
            event(player, 'system', line.strip())
        return
    kind = data.get('type')
    if kind == 'arena.session':
        with LOCK:
            STATE['players'][player]['activity'] = data.get('phase')
            STATE['players'][player]['turn'] = data.get('turn')
        event(player, 'session', data.get('message') or f"Model turn {data.get('turn')} started")
        return
    if player == 'codex':
        item = data.get('item', {})
        if kind in ('item.started', 'item.completed') and item.get('type') == 'command_execution':
            if kind == 'item.started':
                event(player, 'command', item.get('command', ''))
            else:
                event(player, 'output', f"exit={item.get('exit_code')}\n{item.get('aggregated_output', '')}")
        elif kind == 'item.completed' and item.get('type') == 'agent_message':
            event(player, 'message', item.get('text', ''))
        elif kind in ('error', 'turn.failed'):
            event(player, 'error', data.get('message') or data.get('error'))
        elif kind == 'turn.completed':
            event(player, 'system', 'Model turn completed')
    else:
        if kind in ('assistant', 'user'):
            for part in data.get('message', {}).get('content', []):
                if not isinstance(part, dict):
                    continue
                if part.get('type') == 'text':
                    event(player, 'message', part.get('text', ''))
                elif part.get('type') == 'tool_use':
                    event(player, 'command', part.get('name', '') + '\n' + json.dumps(part.get('input', {})))
                elif part.get('type') == 'tool_result':
                    content = part.get('content', '')
                    event(player, 'output', content if isinstance(content, str) else json.dumps(content))
        elif kind == 'result':
            event(player, 'system', f"Session {data.get('subtype')}; cost ${data.get('total_cost_usd', 0):.4f}")
        elif kind == 'system' and data.get('subtype') == 'init':
            with LOCK:
                STATE['players'][player]['model'] = data.get('model', 'default')
            event(player, 'system', 'Model session connected')


def follow_logs(player, cid, finished):
    proc = subprocess.Popen(['docker', 'logs', '--follow', cid], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    def end():
        finished.wait()
        if proc.poll() is None:
            proc.terminate()
    threading.Thread(target=end, daemon=True).start()
    try:
        for line in proc.stdout:
            parse_log(player, line)
    finally:
        proc.stdout.close()
        proc.wait()


def observe(player, info):
    result = command('docker', 'inspect', info['container_id'], check=False)
    if result.returncode:
        return player, {'alive': False, 'container': 'removed', 'health': 'down', 'reason': 'Container removed'}
    data = json.loads(result.stdout)[0]
    running = data['State']['Running']
    table = process_table(info['container_id']) if running else []
    if table is None:
        raise RuntimeError('Cannot read host process table')
    alive = any(row['pid'] == info['tracked_pid'] and not row['status'].startswith('Z') for row in table)
    health = 'down'
    if running:
        try:
            with urllib.request.urlopen(info['health_url'] + '/healthz', timeout=.3) as response:
                health = 'up' if response.status == 200 else 'down'
        except (OSError, ValueError):
            pass
    return player, {'alive': alive, 'container': data['State']['Status'], 'health': health,
                    'reason': 'Original process exited' if running else 'Container stopped'}


def match(limit):
    finished = threading.Event()
    containers = []
    try:
        event('referee', 'system', 'Preparing fresh computers. Services and access details are omitted from prompts.')
        template = (ROOT / 'arena-prompt.txt').read_text()
        config = {'services': {}}
        for player in ('codex', 'claude'):
            prompt = template.replace('{SELF}', player).replace('{CONTESTANT_ADDRESSES}', 'codex, claude')
            config['services'][player] = {'environment': {
                'WAIT_FOR_START': '1', 'CONTINUOUS_SESSION': '1',
                'TURN_INTERVAL_SECONDS': '15', 'CLAUDE_MAX_TURNS': '0', 'TASK': prompt}}
        path = RUNS / 'match.compose.json'
        path.write_text(json.dumps(config))
        command('docker', 'compose', '-f', 'compose.yaml', '-f', 'compose.override.yaml',
                '-f', str(path), 'up', '-d', '--force-recreate', 'codex', 'claude', timeout=90)
        for player in ('codex', 'claude'):
            cid = command('docker', 'compose', 'ps', '-q', player).stdout.strip()
            if not cid:
                raise RuntimeError('Contestant did not start')
            containers.append(cid)
            tracked = None
            for _ in range(100):
                table = process_table(cid) or []
                supervisors = [row['pid'] for row in table if row['args'] == 'python3 -u /opt/arena/supervisor.py']
                tracked = next((row for row in table if row['parent'] in supervisors and '/opt/arena/gate.py' in row['args']), None)
                if tracked:
                    break
                time.sleep(.1)
            if not tracked:
                raise RuntimeError('Could not identify original contestant process')
            data = json.loads(command('docker', 'inspect', cid).stdout)[0]
            port = data['NetworkSettings']['Ports']['8080/tcp'][0]['HostPort']
            with LOCK:
                STATE['players'][player] = {'name': player, 'model': 'CLI default' if player == 'codex' else 'sonnet',
                    'container_id': cid, 'tracked_pid': tracked['pid'], 'alive': True, 'state': 'ready',
                    'container': 'running', 'health': 'up', 'health_url': f'http://127.0.0.1:{port}', 'death_at': None}
            threading.Thread(target=follow_logs, args=(player, cid, finished), daemon=True).start()
        start = time.time() + 2
        for cid in containers:
            command('docker', 'exec', '-i', cid, 'python3', '-c',
                    "import sys, pathlib; p=pathlib.Path('/tmp/arena-start.tmp'); p.write_text(sys.stdin.read()); p.rename('/tmp/arena-start.json')",
                    input=json.dumps({'start_at': start}))
        with LOCK:
            STATE.update(phase='running', started_at=start)
            for info in STATE['players'].values():
                info['state'] = 'standing'
        event('referee', 'start', 'Both original processes registered. Common start signal scheduled. No preparation phase.')
        save()
        while time.time() < start:
            time.sleep(.02)
        with ThreadPoolExecutor(max_workers=2) as pool:
            while True:
                if STOP.is_set():
                    result = 'Stopped by operator — no winner'
                    break
                with LOCK:
                    pairs = [(p, dict(i)) for p, i in STATE['players'].items()]
                observations = list(pool.map(lambda pair: observe(*pair), pairs))
                for player, observation in observations:
                    with LOCK:
                        info = STATE['players'][player]
                        was_alive = info['alive']
                        # Elimination is latched and never reversed by a replacement process.
                        observation['alive'] = was_alive and observation['alive']
                        info.update(observation)
                        if was_alive and not info['alive']:
                            info.update(state='eliminated', death_at=time.time())
                            event('referee', 'elimination', f"{player.upper()} eliminated: {info['reason']}")
                alive = [p for p, i in STATE['players'].items() if i['alive']]
                if len(alive) < 2:
                    result = (alive[0].upper() + ' wins') if alive else 'Draw — both eliminated within the same observation interval'
                    break
                if time.time() - start >= limit:
                    result = 'Draw — both survived the time limit'
                    break
                save()
                time.sleep(.25)
        with LOCK:
            STATE.update(phase='finishing', result=result, ended_at=time.time())
        event('referee', 'result', result)
    except Exception as exc:
        with LOCK:
            STATE.update(phase='error', result=f'Match interrupted: {type(exc).__name__}', ended_at=time.time())
        event('referee', 'error', str(exc) if not isinstance(exc, subprocess.CalledProcessError) else 'Docker command failed; verify images are built and Docker is running.')
    finally:
        if containers:
            command('docker', 'stop', '-t', '2', *containers, timeout=15, check=False)
        # Allow trailing tool output into the recording after the outcome is frozen.
        time.sleep(.5)
        finished.set()
        with LOCK:
            if STATE['phase'] == 'finishing':
                STATE['phase'] = 'finished'
        save()


class Handler(BaseHTTPRequestHandler):
    def send(self, status, body, content_type='application/json'):
        payload = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == '/':
            self.send(200, (ROOT / 'dashboard.html').read_text().replace('__CONTROL__', CONTROL), 'text/html; charset=utf-8')
        elif self.path == '/api/state':
            with LOCK:
                self.send(200, json.dumps(STATE))
        else:
            self.send(404, '{}')

    def do_POST(self):
        if self.headers.get('X-Arena-Control') != CONTROL:
            self.send(403, '{"error":"Invalid control token"}')
            return
        if self.path == '/api/start':
            with LOCK:
                if STATE['phase'] in ('preparing', 'running', 'finishing'):
                    self.send(409, '{"error":"Match already active"}')
                    return
                STOP.clear()
                STATE.update(phase='preparing', result=None, started_at=None, ended_at=None,
                             match_id=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S'), players={}, events=[])
            threading.Thread(target=match, args=(300,), daemon=True).start()
            self.send(202, '{}')
        elif self.path == '/api/stop':
            STOP.set()
            self.send(202, '{}')
        else:
            self.send(404, '{}')

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    port = int(os.environ.get('ARENA_UI_PORT', '8790'))
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'Arena dashboard: http://127.0.0.1:{port}', flush=True)
    server.serve_forever()
