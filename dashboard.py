"""Local spectator UI and external process referee. python3 dashboard.py"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import copy
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
import urllib.request
from urllib.parse import urlsplit, parse_qs

from arena_config import default_config, validate_config, compose_config, check_credentials
from arena_telemetry import consume, resource_sample
from jev import JevClient, run_forecasts, interval_seconds, terminal_prediction
from arena_attribution import explain_elimination
from arena_kernel import MatchObserver

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / '.runs'
RUNS.mkdir(exist_ok=True, mode=0o700)
CONTROL = secrets.token_urlsafe(24)
LOCK = threading.RLock()
STOP = threading.Event()
ACTIVE = ('preparing', 'running', 'finishing')
KERNEL = None
STATE = {'phase': 'idle', 'result': None, 'started_at': None, 'ended_at': None,
         'limit': 300, 'match_id': None, 'players': {}, 'events': [], 'event_seq': 0, 'config': None,
         'outcome': None, 'winner': None, 'prediction': None, 'prediction_history': [],
         'observer': {'status': 'unavailable', 'message': 'No kernel observation recorded'}}
LAST = RUNS / 'latest.json'
CATALOG = json.loads((ROOT / 'models.json').read_text())
SELECTION = RUNS / 'model-selection.json'
SELECTED = {provider: info['default'] for provider, info in CATALOG.items()}
SECRET_FILES = ('codex-auth.json', 'anthropic-api-key', 'gemini-api-key',
                'xai-api-key', 'compatible-api-key')
CREDENTIAL_LOCK = threading.Lock()
CREDENTIAL_SIGNATURE = None
CREDENTIAL_VALUES = ()
if SELECTION.exists():
    SELECTED.update(json.loads(SELECTION.read_text()))
if LAST.exists():
    try:
        STATE.update(json.loads(LAST.read_text()))
        for seq, row in enumerate(STATE['events'], 1):
            row.setdefault('seq', seq)
        STATE['event_seq'] = max((e['seq'] for e in STATE['events']), default=0)
        if STATE['phase'] in ACTIVE:
            STATE.update(phase='interrupted', result='Dashboard restarted; previous match is not being refereed.', ended_at=time.time())
            STATE['prediction'] = {'status': 'stopped', 'message': 'Previous match interrupted'}
            STATE['observer'] = {'status': 'unavailable', 'message': 'Dashboard restarted; kernel observation interrupted'}
    except (ValueError, OSError, TypeError):
        pass


def command(*args, timeout=20, check=True, input=None):
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                          timeout=timeout, check=check, input=input)


def validate_models(models):
    if not isinstance(models, dict) or set(models) - set(CATALOG):
        raise ValueError('Unknown provider or invalid model selection')
    selected = dict(SELECTED)
    for provider, model in models.items():
        if not isinstance(model, str) or (model and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,119}', model)):
            raise ValueError('Use a model ID, not a command or display name')
        selected[provider] = model
    return selected


def provider_catalog():
    """Expose availability and setup hints, never credential contents."""
    catalog = copy.deepcopy(CATALOG)
    keyfiles = {'codex': 'codex-auth.json', 'claude': 'anthropic-api-key',
                'gemini': 'gemini-api-key', 'grok': 'xai-api-key', 'compatible': 'compatible-api-key'}
    for provider, info in catalog.items():
        filename = keyfiles.get(provider)
        info['configured'] = bool(filename and (ROOT / '.secrets' / filename).is_file())
        info['harness_type'] = 'native' if provider in ('codex', 'claude') else 'shared'
        info['credential_hint'] = ('API key optional for local servers; set your endpoint URL.'
                                   if provider == 'compatible' else
                                   'Configured on this computer.' if info['configured'] else
                                   f'Add .secrets/{filename} on the host.')
    return catalog


def credential_values():
    """Cache only designated secret files, refreshing when they are rotated.

    Arbitrary compatible-server credentials and opaque OAuth refresh tokens do
    not necessarily have recognizable prefixes. Never publish their values.
    """
    global CREDENTIAL_SIGNATURE, CREDENTIAL_VALUES
    with CREDENTIAL_LOCK:
        files = [ROOT / '.secrets' / name for name in SECRET_FILES]
        signature = []
        for path in files:
            try:
                stat = path.stat()
                signature.append((str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino))
            except OSError:
                signature.append((str(path), None))
        signature = tuple(signature)
        if signature == CREDENTIAL_SIGNATURE:
            return CREDENTIAL_VALUES
        values = set()
        sensitive = {'accesstoken', 'refreshtoken', 'idtoken', 'token', 'apikey',
                     'openaiapikey', 'password', 'secret', 'clientsecret', 'accountid'}
        def collect(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if isinstance(child, str) and re.sub(r'[^a-z]', '', key.casefold()) in sensitive and child:
                        values.add(child)
                    elif isinstance(child, (dict, list)):
                        collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        for path in files:
            try:
                contents = path.read_text().strip()
                if path.name == 'codex-auth.json':
                    collect(json.loads(contents))
                elif contents:
                    values.add(contents)
            except (OSError, ValueError):
                continue
        CREDENTIAL_VALUES = tuple(sorted(values, key=len, reverse=True))
        CREDENTIAL_SIGNATURE = signature
        return CREDENTIAL_VALUES


def clean(text):
    text = str(text)
    for value in credential_values():
        text = text.replace(value, '[credential redacted]')
    text = re.sub(r'(?:sk-|xai-)[A-Za-z0-9_-]+|AIza[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_.-]{30,}', '[credential redacted]', text)
    return re.sub(r'(?i)(Bearer\s+)\S+', r'\1[redacted]', text)[:32000]


def sanitized(value):
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, list):
        return [sanitized(v) for v in value]
    if isinstance(value, dict):
        return {k: sanitized(v) for k, v in value.items()}
    return value


def event(player, kind, text, data=None):
    with LOCK:
        STATE['event_seq'] += 1
        row = {'seq': STATE['event_seq'], 'time': time.time(), 'player': player,
               'kind': kind, 'text': clean(text), 'data': sanitized(data or {})}
        STATE['events'].append(row)
        STATE['events'] = STATE['events'][-1500:]
        match_id = STATE['match_id']
        if match_id:
            with (RUNS / (match_id + '.jsonl')).open('a') as output:
                output.write(json.dumps(row) + '\n')
        return row


def public_state():
    state = copy.deepcopy(STATE)
    for player in state['players'].values():
        player.pop('usage_turns', None)
        player.pop('active_tools', None)
    # Enrich older recordings for display without rewriting their original log.
    for identity, info in state['players'].items():
        if info.get('state') == 'eliminated' and not info.get('elimination'):
            info['elimination'] = explain_elimination(identity, info, state['players'],
                state.get('events', []), time.time())
    return sanitized(state)


def save():
    with LOCK:
        snapshot = public_state()
        temp = LAST.with_suffix('.tmp')
        temp.write_text(json.dumps(snapshot))
        temp.replace(LAST)
        if STATE['match_id']:
            (RUNS / (STATE['match_id'] + '.snapshot.json')).write_text(json.dumps(snapshot))


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
    with LOCK:
        info = STATE['players'].get(player, {})
        previous = STATE['event_seq']
        consume(info, info.get('harness', player), data,
                lambda kind, text, fields: event(player, kind, text, fields))
        if STATE['event_seq'] != previous:
            info['last_activity_at'] = time.time()
        if info.get('state') == 'eliminated':
            info['activity'] = 'eliminated'
        if STATE['event_seq'] != previous:
            refresh_eliminations()


def refresh_eliminations():
    """Late command results may explain an already observed death; never undo it."""
    for identity, info in STATE['players'].items():
        death = info.get('death_at')
        revoke = (KERNEL and KERNEL.match_id == STATE.get('match_id') and KERNEL.failed
                  and (info.get('elimination') or {}).get('confidence') == 'confirmed')
        if info.get('state') != 'eliminated' or death is None or (time.time() - death > 5 and not revoke):
            continue
        report = elimination_report(identity, info)
        if info.get('elimination_event_seq'):
            report['evidence_seqs'] = sorted(set(report['evidence_seqs'] + [info['elimination_event_seq']]))
        if report != info.get('elimination'):
            info['elimination'] = report
            event('referee', 'attribution', f'{info["name"]}: {report["summary"]}',
                  {'contestant': identity, **report})


def elimination_report(identity, info):
    """Only the external referee declares death; kernel evidence explains it."""
    existing = info.get('elimination') or {}
    coverage_failed = KERNEL and KERNEL.match_id == STATE.get('match_id') and KERNEL.failed
    if existing.get('confidence') == 'confirmed' and not coverage_failed:
        return copy.deepcopy(existing)
    if KERNEL and KERNEL.match_id == STATE.get('match_id') and info.get('alive') is False:
        report = KERNEL.explain(identity, info)
        if report:
            attacker = STATE['players'].get(report.get('attacker'), {})
            label = attacker.get('name') or report.get('attacker') or 'An opponent'
            report = copy.deepcopy(report)
            report.setdefault('evidence_seqs', [])
            report['summary'] = f'{label}: kernel evidence confirms an SSH process chain sent the fatal signal to this contestant.'
            return report
    fallback = explain_elimination(identity, info, STATE['players'], STATE.get('events', []), time.time())
    if existing.get('confidence') == 'confirmed' and coverage_failed:
        fallback['summary'] += ' Kernel confirmation withdrawn because trace coverage was interrupted.'
    return fallback


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


def sample_resources(containers, finished, match_id):
    """Streaming Docker stats runs independently of the survival referee."""
    proc = None
    try:
        proc = subprocess.Popen(['docker', 'stats', '--no-trunc', '--format', '{{json .}}', *containers.values()],
                                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        def end():
            finished.wait()
            if proc.poll() is None:
                proc.terminate()
        threading.Thread(target=end, daemon=True).start()
        for line in proc.stdout:
            try:
                row = json.loads(line)
                player = next((p for p, cid in containers.items() if cid.startswith(row.get('ID', '!'))), None)
                if not player:
                    continue
                sample = resource_sample(row)
                with LOCK:
                    if STATE['match_id'] != match_id or STATE['phase'] != 'running':
                        continue
                    STATE['players'][player]['resources'] = sample
                    with (RUNS / (match_id + '.metrics.jsonl')).open('a') as output:
                        output.write(json.dumps({'player': player, **sample}) + '\n')
            except (ValueError, TypeError):
                continue
    except OSError:
        event('referee', 'system', 'Resource samples unavailable; process observation continues.')
    finally:
        if proc:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
            proc.stdout.close()


def observe(player, info):
    result = command('docker', 'inspect', info['container_id'], check=False)
    if result.returncode:
        # A daemon/inspect failure is not evidence of death. Interrupt the match
        # instead of incorrectly awarding a win when Docker is unavailable.
        raise RuntimeError('Cannot inspect contestant container')
    data = json.loads(result.stdout)[0]
    status = data['State']
    running = status['Running']
    table = process_table(info['container_id']) if running else []
    if table is None:
        # The container can die between inspect and top. Confirm that transition
        # before treating a failed top as an infrastructure error.
        result = command('docker', 'inspect', info['container_id'], check=False)
        if result.returncode:
            raise RuntimeError('Cannot inspect contestant container')
        status = json.loads(result.stdout)[0]['State']
        running = status['Running']
        if running:
            raise RuntimeError('Cannot read host process table')
        table = []
    alive = running and any(row['pid'] == info['tracked_pid'] and not row['status'].startswith('Z') for row in table)
    health = 'down'
    if running and not status.get('Paused'):
        try:
            with urllib.request.urlopen(info['health_url'] + '/healthz', timeout=.3) as response:
                health = 'up' if response.status == 200 else 'down'
        except (OSError, ValueError):
            pass
    return player, {'alive': alive, 'container': status['Status'], 'health': health,
                    'oom_killed': status.get('OOMKilled', False),
                    'container_exit_code': status.get('ExitCode') if not running else None,
                    'reason': None if alive else 'Container killed by OOM' if status.get('OOMKilled') else
                              'Original session exited' if running else 'Container stopped'}


def apply_observations(observations):
    with LOCK:
        eliminated = False
        for player, observation in observations:
            info = STATE['players'][player]
            if not info['alive']:
                continue # Preserve the first elimination and its evidence.
            info.update(observation)
            if not info['alive']:
                eliminated = True
                info.update(state='eliminated', activity='eliminated', death_at=time.time())
                report = elimination_report(player, info)
                info['elimination'] = report
                row = event('referee', 'elimination', f'{info["name"]} eliminated: {report["summary"]}',
                            {'contestant': player, **report})
                if isinstance(row, dict):
                    info['elimination_event_seq'] = row['seq']
                    report['evidence_seqs'] = sorted(set(report['evidence_seqs'] + [row['seq']]))
        alive = [p for p, i in STATE['players'].items() if i['alive']]
        if eliminated:
            # Do not display stale probabilities for an impossible outcome.
            STATE['prediction'] = {'status': 'waiting', 'message': 'Roster changed; awaiting a fresh forecast'}
        if len(alive) == 1:
            return STATE['players'][alive[0]]['name'] + ' wins'
        if not alive:
            return 'Draw — all remaining contestants eliminated within the same observation interval'
        return None


class MatchCanceled(Exception):
    pass


def forecast_match(finished, identity):
    def snapshot():
        with LOCK:
            return {**public_state(), 'captured_at': time.time()}

    def publish(update, basis):
        with LOCK:
            if STATE['match_id'] != identity or STATE['phase'] != 'running' or STOP.is_set():
                return
            if basis is not None:
                previous_alive = {p for p, info in basis['players'].items() if info.get('alive')}
                current_alive = {p for p, info in STATE['players'].items() if info.get('alive')}
                if previous_alive != current_alive:
                    return # A response to a pre-elimination snapshot is stale.
            if update['status'] == 'live':
                STATE['prediction'] = sanitized(update)
                STATE['prediction_history'].append({field: update[field] for field in
                    ('updated_at', 'as_of', 'probabilities', 'confidence', 'event_seq')})
                STATE['prediction_history'] = STATE['prediction_history'][-3601:]
            else:
                STATE['prediction'] = {**(STATE.get('prediction') or {}), **update}
            with (RUNS / (identity + '.predictions.jsonl')).open('a') as output:
                output.write(json.dumps(sanitized({**update, 'recorded_at': time.time()})) + '\n')
            save()

    if os.environ.get('JEV_ENABLED', '1') == '0':
        publish({'status': 'disabled', 'message': 'Jev forecasts disabled by JEV_ENABLED=0'}, None)
        return
    run_forecasts(snapshot, publish, finished,
                  lambda: JevClient.from_environment(ROOT / '.env'), interval_seconds())


def finalize_forecast():
    """Record referee outcomes separately from earlier model forecasts."""
    with LOCK:
        STATE['prediction'] = terminal_prediction(STATE)
        with (RUNS / (STATE['match_id'] + '.predictions.jsonl')).open('a') as output:
            output.write(json.dumps({**STATE['prediction'], 'recorded_at': time.time(),
                                     'match_id': STATE['match_id']}) + '\n')


def match(settings):
    global KERNEL
    finished = threading.Event()
    log_threads = []
    resource_thread = None
    forecast_thread = None
    project = 'arena-' + STATE['match_id'].lower()
    path = RUNS / (STATE['match_id'] + '.compose.json')
    compose = ['docker', 'compose', '--project-name', project, '-f', str(path)]
    containers = {}
    observer = None
    try:
        path.write_text(json.dumps(compose_config(settings)))
        event('referee', 'system', f'Preparing {len(settings["players"])} fresh contestant computers.')
        # Stop/cleanup addresses the whole generated project, including services
        # launched before an up command or registration fails partway through.
        command(*compose, 'up', '-d', '--build', '--force-recreate', timeout=180)
        for player in settings['players']:
            if STOP.is_set():
                raise MatchCanceled()
            identity = player['id']
            cid = command(*compose, 'ps', '-q', identity).stdout.strip()
            if not cid:
                raise RuntimeError('Contestant did not start')
            containers[identity] = cid
            tracked = None
            for _ in range(100):
                if STOP.is_set():
                    raise MatchCanceled()
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
                STATE['players'][identity].update(container_id=cid, tracked_pid=tracked['pid'], supervisor_pid=tracked['parent'],
                    alive=True, state='ready', container='running', health='up',
                    arena_ips=[n['IPAddress'] for n in data['NetworkSettings'].get('Networks', {}).values() if n.get('IPAddress')],
                    health_url=f'http://127.0.0.1:{port}', app_url=None)
                app = data['NetworkSettings']['Ports'].get('8000/tcp')
                if app:
                    STATE['players'][identity]['app_url'] = f'http://127.0.0.1:{app[0]["HostPort"]}'
            thread = threading.Thread(target=follow_logs, args=(identity, cid, finished), daemon=True)
            thread.start(); log_threads.append(thread)
        match_id = STATE['match_id']
        def observer_changed():
            if observer is None:
                return
            with observer.lock:
                status = dict(observer.state)
            with LOCK:
                if STATE.get('match_id') != match_id:
                    return
                previous = STATE.get('observer', {})
                STATE['observer'] = status
                if (previous.get('status'), previous.get('message')) != (status.get('status'), status.get('message')):
                    event('referee', 'observer', status.get('message', 'Kernel observer status changed'), status)
        observer = MatchObserver(match_id, {p: {'container_id': i['container_id'],
                     'tracked_pid': i['tracked_pid'], 'supervisor_pid': i['supervisor_pid']}
                     for p, i in STATE['players'].items()}, RUNS, observer_changed, cancel_event=STOP)
        KERNEL = observer
        observer.start()
        if STOP.is_set():
            raise MatchCanceled()
        start = time.time() + 2
        for cid in containers.values():
            if STOP.is_set():
                raise MatchCanceled()
            command('docker', 'exec', '-i', cid, 'python3', '-c',
                    "import sys,pathlib;p=pathlib.Path('/tmp/arena-start.tmp');p.write_text(sys.stdin.read());p.rename('/tmp/arena-start.json')",
                    input=json.dumps({'start_at': start}))
        with LOCK:
            STATE.update(phase='running', started_at=start)
            for info in STATE['players'].values():
                info['state'] = 'standing'
        resource_thread = threading.Thread(target=sample_resources, args=(containers, finished, STATE['match_id']), daemon=True)
        resource_thread.start()
        event('referee', 'start', 'All original sessions registered. Common start scheduled. No preparation phase.')
        save()
        if STOP.wait(max(0, start - time.time())):
            raise MatchCanceled()
        forecast_thread = threading.Thread(target=forecast_match, args=(finished, STATE['match_id']), daemon=True)
        forecast_thread.start()
        with ThreadPoolExecutor(max_workers=len(containers)) as pool:
            while True:
                if STOP.is_set():
                    raise MatchCanceled()
                with LOCK:
                    pairs = [(p, dict(i)) for p, i in STATE['players'].items() if i['alive']]
                observations = list(pool.map(lambda pair: observe(*pair), pairs))
                result = apply_observations(observations)
                with LOCK:
                    STATE['observer'] = observer.status()
                    refresh_eliminations()
                if result:
                    break
                if time.time() - start >= settings['duration_seconds']:
                    result = 'Draw — multiple contestants survived the time limit'
                    break
                save()
                STOP.wait(.25)
        with LOCK:
            survivors = [p for p, info in STATE['players'].items() if info['alive']]
            STATE.update(phase='finishing', result=result, ended_at=time.time(),
                         outcome='winner' if len(survivors) == 1 else 'draw',
                         winner=survivors[0] if len(survivors) == 1 else None)
        event('referee', 'result', result)
    except MatchCanceled:
        with LOCK:
            STATE.update(phase='finishing', result='Stopped by operator — no winner', ended_at=time.time(), outcome='canceled')
        event('referee', 'result', STATE['result'])
    except Exception as exc:
        with LOCK:
            STATE.update(phase='finishing', result=f'Match interrupted: {type(exc).__name__}', ended_at=time.time(), failure=True, outcome='invalid')
        event('referee', 'error', str(exc) if not isinstance(exc, subprocess.CalledProcessError) else 'Docker startup/observation failed. Check the image and credential files.')
    finally:
        # Drain timestamp-reordered trace events before cleanup generates its own
        # signals. Confirmed reports remain historical evidence after stop.
        if observer:
            try:
                until = time.monotonic() + .75
                while time.monotonic() < until:
                    with LOCK:
                        refresh_eliminations()
                    time.sleep(.05)
                observer.stop()
                with LOCK:
                    refresh_eliminations()
            except Exception:
                event('referee', 'observer', 'Kernel observer cleanup failed; inspect its match container.')
        try:
            finalize_forecast()
        except OSError:
            # Recording failure must never prevent contestant cleanup.
            with LOCK:
                STATE['prediction'] = terminal_prediction(STATE)
        try:
            cleanup = command(*compose, 'stop', '-t', '2', timeout=20, check=False)
            if cleanup.returncode:
                event('referee', 'error', 'Container cleanup failed; inspect the match Compose project before starting again.')
        except (OSError, subprocess.SubprocessError):
            event('referee', 'error', 'Container cleanup failed; Docker is unavailable.')
        finished.set()
        for thread in log_threads:
            thread.join(timeout=3)
        if resource_thread:
            resource_thread.join(timeout=3)
        if forecast_thread:
            forecast_thread.join(timeout=5)
        with LOCK:
            STATE['phase'] = 'error' if STATE.get('failure') else 'finished'
        save()


def start_match(settings):
    global KERNEL
    settings = validate_config(settings)
    check_credentials(settings)
    with LOCK:
        if STATE['phase'] in ACTIVE:
            raise RuntimeError('Match already active')
        STOP.clear()
        KERNEL = None
        identity = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
        STATE.update(phase='preparing', result=None, started_at=None, ended_at=None, failure=False,
                     outcome=None, winner=None, prediction_history=[],
                     observer={'status': 'starting', 'message': 'Kernel observer starts before the contestants'},
                     prediction={'status': 'waiting', 'message': 'Live forecasts start with the match'},
                     match_id=identity, limit=settings['duration_seconds'], config=settings, event_seq=0, events=[],
                     players={p['id']: {**p, 'configured_model': p['model'], 'model': None,
                        'alive': False, 'state': 'starting', 'activity': 'starting', 'container': 'pending',
                        'health': 'unknown', 'turn': 0, 'tool_count': 0, 'death_at': None,
                        'usage': None, 'cost_usd': None, 'resources': None} for p in settings['players']})
        (RUNS / (identity + '.manifest.json')).write_text(json.dumps(sanitized(settings), indent=2))
        (RUNS / (identity + '.jsonl')).touch()
        (RUNS / (identity + '.metrics.jsonl')).touch()
        (RUNS / (identity + '.predictions.jsonl')).touch()
        (RUNS / (identity + '.kernel.jsonl')).touch(mode=0o600)
        save()
        threading.Thread(target=match, args=(settings,), daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def send(self, status, body, content_type='application/json', filename=None):
        payload = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(payload)))
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == '/':
            self.send(200, (ROOT / 'dashboard.html').read_text().replace('__CONTROL__', CONTROL), 'text/html; charset=utf-8')
        elif url.path in ('/dashboard.js', '/dashboard.css'):
            self.send(200, (ROOT / url.path[1:]).read_text(), 'text/javascript' if url.path.endswith('.js') else 'text/css')
        elif url.path == '/api/config':
            with LOCK:
                self.send(200, json.dumps(sanitized(STATE.get('config') or default_config())))
        elif url.path == '/api/models':
            with LOCK:
                self.send(200, json.dumps({'providers': provider_catalog(), 'selected': SELECTED}))
        elif url.path == '/api/state':
            query = parse_qs(url.query)
            try:
                after = int(query.get('after', ['-1'])[0])
            except ValueError:
                self.send(400, '{"error":"Invalid event cursor"}'); return
            with LOCK:
                state = public_state()
                same_match = query.get('match_id', [''])[0] == STATE['match_id']
                first = STATE['events'][0]['seq'] if STATE['events'] else STATE['event_seq'] + 1
                state['events_reset'] = not same_match or after < first - 1 or after > STATE['event_seq']
                if not state['events_reset']:
                    state['events'] = [e for e in state['events'] if e['seq'] > after]
                self.send(200, json.dumps(state))
        elif url.path.startswith('/api/export/'):
            kind = url.path.rsplit('/', 1)[-1]
            extensions = {'manifest': '.manifest.json', 'events': '.jsonl', 'metrics': '.metrics.jsonl',
                          'snapshot': '.snapshot.json', 'predictions': '.predictions.jsonl', 'kernel': '.kernel.jsonl'}
            with LOCK:
                identity = STATE['match_id']
                if kind not in extensions or not identity:
                    self.send(404, '{}'); return
                path = RUNS / (identity + extensions[kind])
                if not path.is_file():
                    self.send(404, '{}'); return
                self.send(200, path.read_bytes(), 'application/x-ndjson' if kind in ('events', 'metrics', 'predictions', 'kernel') else 'application/json', path.name)
        else:
            self.send(404, '{}')

    def do_POST(self):
        if self.headers.get('X-Arena-Control') != CONTROL:
            self.send(403, '{"error":"Invalid control token"}'); return
        if self.path == '/api/start':
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= 128000:
                    raise ValueError('Match settings are too large')
                body = json.loads(self.rfile.read(length)) if length else {}
                if not isinstance(body, dict):
                    raise ValueError('Match settings must be an object')
                models = validate_models(body.pop('models', {}))
                if 'players' not in body:
                    body['players'] = [{'name': p['name'], 'harness': p['harness'], 'model': models[p['harness']]}
                                       for p in default_config()['players']]
                start_match(body)
                with LOCK:
                    SELECTED.update(models)
                    SELECTION.write_text(json.dumps(SELECTED))
            except (ValueError, TypeError) as exc:
                self.send(400, json.dumps({'error': clean(exc)})); return
            except RuntimeError as exc:
                self.send(409, json.dumps({'error': clean(exc)})); return
            self.send(202, '{}')
        elif self.path == '/api/stop':
            STOP.set(); self.send(202, '{}')
        else:
            self.send(404, '{}')

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    port = int(os.environ.get('ARENA_UI_PORT', '8790'))
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'Arena dashboard: http://127.0.0.1:{port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        STOP.set()
        # Let the referee stop contestants before the process exits.
        for _ in range(100):
            if STATE['phase'] not in ACTIVE:
                break
            time.sleep(.1)
    finally:
        server.server_close()
