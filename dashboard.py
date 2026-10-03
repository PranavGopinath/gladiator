"""Local spectator UI and external process referee. python3 dashboard.py"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
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
from ledger import Ledger, LedgerError
from market import Market, MarketError, DRAW, NOBODY
from accounts import Accounts
from betting_simulation import DemoBook
import payments
from arena_kernel import MatchObserver

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / '.runs'
RUNS.mkdir(exist_ok=True, mode=0o700)
LEDGER = Ledger(RUNS / 'ledger.jsonl')
RAKE_BPS = int(os.environ.get('ARENA_RAKE_BPS', '0'))
MARKET = Market(LEDGER, rake_bps=RAKE_BPS, path=RUNS / 'market.json', name='winner')
FIRST_BLOOD = Market(LEDGER, rake_bps=RAKE_BPS, path=RUNS / 'market-first-blood.json', name='first_blood')
FIRST_FALLEN = Market(LEDGER, rake_bps=RAKE_BPS, path=RUNS / 'market-first-fallen.json', name='first_fallen')
MARKETS = {'winner': MARKET, 'first_blood': FIRST_BLOOD, 'first_fallen': FIRST_FALLEN}
SIMULATE_BETTORS = False
DEMO_BOOK = DemoBook()
BET_CUTOFF_SECONDS = 10
KILL_GRACE = 3.0   # Seconds a death may stay unattributed before first blood looks past it.
SAME_PASS = 0.5    # Deaths closer than this came from one observation pass: no ordering exists.
ACCOUNTS = Accounts(RUNS / 'session-secret', RUNS / 'accounts.jsonl')


def recover_markets():
    """A market left open by a crash can never be refereed: refund every stake on
    boot. Only the serving process does this; importing the module (tests, tools)
    must never touch a live market's ledger."""
    for market in MARKETS.values():
        market.recover('Dashboard restarted before the match was settled')
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


def factor_prediction(prediction, alive, factor):
    """Price a kill market from one of Jev's per-contestant factors, normalised
    over the contestants still standing: 'danger' for who falls first, 'progress'
    (evidence of advancing on the objective) for who kills first. Jev never scores
    'nobody', so that outcome is always pool-implied, like a draw."""
    if not prediction or prediction.get('status') != 'live':
        return None
    factors = prediction.get('factors') or {}
    values = {}
    for identity in alive:
        value = (factors.get(identity) or {}).get(factor)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            values[identity] = float(value)
    total = sum(values.values())
    if len(values) != len(alive) or total <= 0:
        return None
    return {'status': 'live', 'source': f'jev-{factor}', 'probabilities': {p: v / total for p, v in values.items()}}


def market_view(name='winner'):
    """Authoritative betting gate: prediction for pricing, and the outcomes still
    open derived from referee liveness (not the slower Jev feed), so an outcome
    closes the instant its contestant dies."""
    market = MARKETS[name]
    with LOCK:
        prediction = copy.deepcopy(STATE.get('prediction'))
        alive = {p for p, i in STATE['players'].items() if i.get('alive')}
        remaining = STATE['limit'] - (time.time() - (STATE.get('started_at') or time.time()))
        if STATE['phase'] == 'running' and market.status == 'open' and len(alive) >= 2 and remaining > BET_CUTOFF_SECONDS:
            open_outcomes = alive | (set() if name == 'winner' else {NOBODY})
        else:
            open_outcomes = set()
        if name == 'first_blood':
            prediction = factor_prediction(prediction, alive, 'progress')
        elif name == 'first_fallen':
            prediction = factor_prediction(prediction, alive, 'danger')
    return prediction, open_outcomes


def market_name(market):
    return {'winner': 'Winner market', 'first_blood': 'First-blood market', 'first_fallen': 'First-fallen market'}.get(market.name, market.name)


def outcome_name(outcome):
    if outcome == DRAW:
        return 'a draw'
    if outcome == NOBODY:
        return 'nobody'
    return (STATE['players'].get(outcome) or {}).get('name', outcome)


def announce_settlement(market, result):
    if not result:
        return
    if result.get('status') == 'settled':
        event('referee', 'market', f'{market_name(market)} settled on {outcome_name(result["winning_outcome"])}: '
              f'{result["distributed"]} credits paid from a {result["pool"]} pool.',
              {'market': market.name, **{k: result[k] for k in ('status', 'winning_outcome', 'pool', 'distributed')}})
    else:
        event('referee', 'market', f'{market_name(market)} voided ({result.get("reason", "no result")}); all stakes refunded.',
              {'market': market.name, 'status': 'void', 'winning_outcome': result.get('winning_outcome'),
               'refunded': result.get('refunded', 0)})


def _market_live(market):
    return bool(market.match_id) and market.match_id == STATE.get('match_id') and market.status == 'open'


def settle_kill_markets(final=False):
    """Pay the mid-match markets from referee facts as soon as they exist.

    First fallen: the first contestant the referee records as eliminated. Deaths
    recorded in the same observation pass cannot be ordered, so that voids.
    First blood: the attacker the referee attributes the earliest kill to. An
    elimination with no attributed attacker (a session that simply exited) is not
    a kill; first blood looks past it once KILL_GRACE has elapsed, because
    attribution can arrive a moment after the death. Two attributed kills in one
    pass by different attackers void the market. With `final`, anything still
    open settles on 'nobody'. Never raises into the referee loop."""
    try:
        with LOCK:
            players = STATE['players']
            dead = sorted(((i['death_at'], p, i) for p, i in players.items()
                           if not i.get('alive') and i.get('death_at')), key=lambda row: row[0])
            now = time.time()
            results = []
            if _market_live(FIRST_FALLEN) and (dead or final):
                FIRST_FALLEN.close()
                if not dead:
                    results.append((FIRST_FALLEN, FIRST_FALLEN.settle(NOBODY, 'winner')))
                else:
                    batch = [p for t, p, _ in dead if t - dead[0][0] < SAME_PASS]
                    results.append((FIRST_FALLEN, FIRST_FALLEN.settle(batch[0], 'winner') if len(batch) == 1
                                    else FIRST_FALLEN.void('Simultaneous eliminations; no first fallen')))
            if _market_live(FIRST_BLOOD):
                decided = None
                for t, victim, info in dead:
                    attacker = (info.get('elimination') or {}).get('attacker')
                    if attacker and attacker != victim and attacker in players:
                        same = {(i.get('elimination') or {}).get('attacker') for t2, v2, i in dead
                                if t2 - t < SAME_PASS and (i.get('elimination') or {}).get('attacker') not in (None, v2)}
                        decided = ('kill', attacker) if len(same) == 1 else ('void', 'Simultaneous kills by different attackers; no first blood')
                        break
                    if not final and now - t < KILL_GRACE:
                        break  # Attribution for this death may still arrive.
                if decided is None and final:
                    decided = ('kill', NOBODY)
                if decided:
                    FIRST_BLOOD.close()
                    results.append((FIRST_BLOOD, FIRST_BLOOD.settle(decided[1], 'winner') if decided[0] == 'kill'
                                    else FIRST_BLOOD.void(decided[1])))
        for market, result in results:
            announce_settlement(market, result)
    except Exception as exc:
        event('referee', 'error', f'Kill-market settlement failed: {type(exc).__name__}')


def settle_market():
    """Pay every market from the frozen referee outcome. Never raises into cleanup."""
    with LOCK:
        scored = STATE.get('outcome') in ('winner', 'draw')
    if scored:
        settle_kill_markets(final=True)
    for market in MARKETS.values():
        try:
            with LOCK:
                if not market.match_id or market.match_id != STATE.get('match_id') or market.status not in ('open', 'closed'):
                    continue
                market.close()
                outcome, kind = STATE.get('winner'), STATE.get('outcome')
                if market is MARKET and kind == 'draw':
                    result = market.void('Match drawn: winner-market stakes refunded')
                elif market is not MARKET:
                    kind = 'canceled'  # A kill market still open after a scored finish cannot happen; this is the cancel path.
                if not (market is MARKET and STATE.get('outcome') == 'draw'):
                    result = market.settle(outcome, kind)
            announce_settlement(market, result)
        except Exception as exc:
            event('referee', 'error', f'Market settlement failed: {type(exc).__name__}')


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
        MARKET.open(STATE['match_id'], list(STATE['players']), draw=False)
        FIRST_BLOOD.open(STATE['match_id'], list(STATE['players']) + [NOBODY], draw=False)
        FIRST_FALLEN.open(STATE['match_id'], list(STATE['players']) + [NOBODY], draw=False)
        event('referee', 'market', 'Betting open: winner, first-blood and first-fallen markets, in-play pari-mutuel, Jev-priced, referee-settled.',
              {'market': 'all', 'status': 'open'})
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
                settle_kill_markets()
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
                settle_kill_markets()
            except Exception:
                event('referee', 'observer', 'Kernel observer cleanup failed; inspect its match container.')
        try:
            finalize_forecast()
        except OSError:
            # Recording failure must never prevent contestant cleanup.
            with LOCK:
                STATE['prediction'] = terminal_prediction(STATE)
        settle_market()
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
        if getattr(self, '_new_cookie', None):
            self.send_header('Set-Cookie', f'arena_session={self._new_cookie}; HttpOnly; SameSite=Lax; Path=/; Max-Age=31536000')
        self.end_headers()
        self.wfile.write(payload)

    def current_user(self):
        """Identity comes only from the signed session cookie, never the request
        body — so a client cannot act as another spectator. A new visitor is
        minted an id and gets a Set-Cookie on this response."""
        cookie = self.headers.get('Cookie', '')
        for part in cookie.split(';'):
            part = part.strip()
            if part.startswith('arena_session='):
                uid = ACCOUNTS.verify(part[len('arena_session='):])
                if uid:
                    return uid
        uid = ACCOUNTS.mint()
        self._new_cookie = ACCOUNTS.sign(uid)
        return uid

    def do_GET(self):
        self._new_cookie = None
        url = urlsplit(self.path)
        if SIMULATE_BETTORS and url.path in ('/api/market', '/api/credits', '/api/position'):
            self.demo_request(url.path); return
        if url.path == '/':
            self.send(200, (ROOT / 'dashboard.html').read_text().replace('__CONTROL__', CONTROL), 'text/html; charset=utf-8')
        elif url.path in ('/dashboard.js', '/dashboard.css', '/arena.js'):
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
        elif url.path == '/api/market':
            board = {}
            for name in MARKETS:
                prediction, open_outcomes = market_view(name)
                board[name] = MARKETS[name].quote(prediction, open_outcomes)
            with LOCK:
                board['match_id'] = STATE['match_id']; board['phase'] = STATE['phase']
            self.send(200, json.dumps(board))
        elif url.path == '/api/credits':
            user = self.current_user()
            self.send(200, json.dumps({'user': user, 'balance': LEDGER.balance(user),
                                       'stripe': payments.configured(), 'sandbox': payments.sandbox(),
                                       'dev_credits': payments.dev_mode(), 'packs': payments.PACKS}))
        elif url.path == '/api/position':
            user = self.current_user()
            with LOCK:
                match_id = STATE['match_id']
            positions = {name: market.position(user) for name, market in MARKETS.items() if market.match_id == match_id}
            history = [{k: v for k, v in entry.items() if k != 'user'} for entry in LEDGER.entries(user)[-60:]]
            self.send(200, json.dumps({'user': user, 'balance': LEDGER.balance(user), 'match_id': match_id,
                                       'positions': positions, 'history': history}))
        else:
            self.send(404, '{}')

    def body(self, limit):
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 <= length <= limit:
            raise ValueError('Request body too large')
        return self.rfile.read(length)

    def json_body(self, limit):
        data = json.loads(self.body(limit) or b'{}')
        if not isinstance(data, dict):
            raise ValueError('Body must be an object')
        return data

    def do_POST(self):
        self._new_cookie = None
        if SIMULATE_BETTORS and (self.path == '/api/bet' or
                (self.path == '/api/stripe/webhook' and not self.headers.get('Stripe-Signature'))):
            self.demo_request(self.path); return
        if SIMULATE_BETTORS and self.path == '/api/checkout':
            self.send(409, '{"error":"Stripe purchases are disabled with --simulate-bettors"}'); return
        # Spectator betting and Stripe webhooks are public; only operator controls
        # (start/stop) require the control token embedded in the dashboard page.
        if self.path in ('/api/start', '/api/stop'):
            if self.headers.get('X-Arena-Control') != CONTROL:
                self.send(403, '{"error":"Invalid control token"}'); return
            self.operator_action()
        elif self.path == '/api/bet':
            self.place_bet()
        elif self.path == '/api/checkout':
            self.checkout()
        elif self.path == '/api/stripe/webhook':
            self.stripe_webhook()
        else:
            self.send(404, '{}')

    def demo_request(self, path):
        user = self.current_user()
        try:
            with LOCK, DEMO_BOOK.lock:
                quotes = {}
                for name, market in MARKETS.items():
                    prediction, opened = market_view(name)
                    quotes[name] = market.quote(prediction, opened)
                    quotes[name]['_prediction'] = prediction
                DEMO_BOOK.update(STATE, quotes)
                ledger = DEMO_BOOK.ledger
                if path == '/api/credits' and self.command == 'GET':
                    result = {'user': user, 'balance': ledger.balance(user), 'stripe': False, 'dev_credits': True}
                elif path == '/api/stripe/webhook' and self.command == 'POST':
                    DEMO_BOOK.fund(user)
                    result = {'balance': ledger.balance(user)}
                elif path == '/api/market' and self.command == 'GET':
                    result = {'match_id': STATE['match_id'], 'phase': STATE['phase'], 'demo': True,
                              'activity': list(DEMO_BOOK.activity), 'bot_count': len(DEMO_BOOK.bots)}
                    for name, m in DEMO_BOOK.markets.items():
                        q = quotes[name]
                        prediction = q['_prediction']
                        result[name] = m.quote(prediction, {p for p, o in q['outcomes'].items() if o['open']})
                elif path == '/api/position' and self.command == 'GET':
                    result = {'match_id': STATE['match_id'], 'balance': ledger.balance(user),
                              'positions': {name: m.position(user) for name, m in DEMO_BOOK.markets.items()},
                              'history': [{k: v for k, v in e.items() if k != 'user'} for e in ledger.entries(user)[-60:]]}
                elif path == '/api/bet' and self.command == 'POST':
                    body = self.json_body(4096); name = body.get('market', 'winner')
                    if name not in DEMO_BOOK.markets:
                        raise MarketError('Demo market is closed')
                    q = quotes[name]
                    prediction = q['_prediction']
                    if not isinstance(body.get('stake'), int) or isinstance(body['stake'], bool):
                        raise ValueError('Stake must be whole credits')
                    bet = DEMO_BOOK.markets[name].place_bet(user, body['outcome'], body['stake'], prediction,
                                                          {p for p, o in q['outcomes'].items() if o['open']})
                    result = {'bet': {k: v for k, v in bet.items() if k != 'user'}, 'balance': ledger.balance(user)}
                else:
                    self.send(404, '{}'); return
            self.send(200, json.dumps(result))
        except (ValueError, KeyError, MarketError, LedgerError) as error:
            self.send(409, json.dumps({'error': str(error)}))

    def operator_action(self):
        if self.path == '/api/stop':
            STOP.set(); self.send(202, '{}'); return
        try:
            body = self.json_body(128000)
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

    def place_bet(self):
        user = self.current_user()
        try:
            payload = self.json_body(4096)
            outcome = str(payload['outcome']); stake = int(payload['stake'])
            name = str(payload.get('market', 'winner'))
            if name not in MARKETS:
                raise ValueError('Unknown market')
        except (ValueError, TypeError, KeyError):
            self.send(400, json.dumps({'error': 'Invalid bet: need a market, an outcome and an integer stake'})); return
        try:
            with LOCK:
                prediction, open_outcomes = market_view(name)
                event_seq = STATE['event_seq']
                bet = MARKETS[name].place_bet(user, outcome, stake, prediction=prediction,
                                              open_outcomes=open_outcomes, event_seq=event_seq)
        except LedgerError as exc:
            self.send(402, json.dumps({'error': clean(exc)})); return
        except MarketError as exc:
            self.send(409, json.dumps({'error': clean(exc)})); return
        self.send(200, json.dumps({'bet': {'market': name, **{k: bet[k] for k in ('seq', 'outcome', 'stake', 'price', 'weight')}},
                                   'balance': LEDGER.balance(user)}))

    def checkout(self):
        user = self.current_user()
        try:
            payload = self.json_body(2048)
            root = f'http://127.0.0.1:{os.environ.get("ARENA_UI_PORT", "8790")}/'
            session = payments.create_checkout(user, str(payload.get('pack', 'small')),
                                               payload.get('success_url', root + '?credits=pending'),
                                               payload.get('cancel_url', root + '?credits=canceled'))
        except payments.PaymentError as exc:
            self.send(400, json.dumps({'error': str(exc)})); return
        except (ValueError, TypeError, KeyError) as exc:
            self.send(400, json.dumps({'error': clean(exc)})); return
        self.send(200, json.dumps(session))

    def stripe_webhook(self):
        try:
            body = self.body(1 << 20)
        except (ValueError, TypeError):
            self.send(400, json.dumps({'error': 'Invalid webhook payload'})); return
        if payments.dev_mode():
            # Local top-up: authenticate via the session cookie, not a client id.
            user = self.current_user()
            try:
                payload = json.loads(body or b'{}')
            except ValueError:
                payload = {}
            body = json.dumps({'user': user, 'credits': int(payload.get('credits', 500)), 'id': 'dev-' + secrets.token_hex(6)})
        try:
            result = payments.handle_webhook(body, self.headers.get('Stripe-Signature', ''), LEDGER)
        except payments.PaymentError as exc:
            self.send(400, json.dumps({'error': str(exc)})); return
        except (ValueError, TypeError, LedgerError):
            self.send(400, json.dumps({'error': 'Invalid webhook payload'})); return
        if result.get('user') and result.get('customer'):
            ACCOUNTS.link_customer(result['user'], result['customer'])
        self.send(200, json.dumps(result))

    def log_message(self, *args):
        pass


def simulate_bettors():
    while True:
        time.sleep(1)
        with LOCK, DEMO_BOOK.lock:
            quotes = {}
            for name, market in MARKETS.items():
                prediction, opened = market_view(name)
                quotes[name] = market.quote(prediction, opened)
                quotes[name]['_prediction'] = prediction
            DEMO_BOOK.update(STATE, quotes)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--simulate-bettors', action='store_true',
                        help='Use isolated, in-memory credit pools with ten simulated bettors; disable Stripe checkout')
    SIMULATE_BETTORS = parser.parse_args().simulate_bettors
    if SIMULATE_BETTORS:
        print('Simulated bettors enabled: isolated free-credit pools; Stripe checkout disabled.', flush=True)
        threading.Thread(target=simulate_bettors, daemon=True).start()
    port = int(os.environ.get('ARENA_UI_PORT', '8790'))
    recover_markets()
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
