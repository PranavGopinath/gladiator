"""Conservative elimination explanations from host observations and public logs.

Tool results and contestant errors are untrusted reports. A matching remote kill
can be probable evidence of an attack, never proof of who caused a death.
"""
import math
import re
import shlex

WINDOW_SECONDS = 15
COMPLETION_GRACE_SECONDS = 2


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _tokens(command):
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|()<>\n')
        lexer.whitespace = ' \t\r'
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return []


def _separator(token):
    return bool(token) and all(character in ';&|()\n' for character in token)


def _destructive(command):
    """Find executed kill commands, excluding probes and SSH hardening."""
    tokens = _tokens(command)
    command_position = True
    for index, token in enumerate(tokens):
        if _separator(token):
            command_position = True
            continue
        if not command_position:
            continue
        if token in ('then', 'do', 'else', 'if', 'while', 'until', 'sudo', 'env') or token.startswith('-') or re.match(r'^\w+=', token):
            continue
        command_position = False
        executable = token.rsplit('/', 1)[-1]
        if executable not in ('kill', 'pkill', 'killall'):
            continue
        args = []
        for argument in tokens[index + 1:]:
            if _separator(argument):
                break
            args.append(argument)
        if not args or any(argument in ('-0', '-l', '-L', '--list', '--table', '-s0', '-n0') for argument in args):
            continue
        if any(args[offset] in ('-s', '-n', '--signal') and args[offset + 1].upper() in ('0', 'ZERO')
               for offset in range(len(args) - 1)):
            continue
        if executable in ('pkill', 'killall') and any(argument.strip('^$') in ('ssh', 'sshd', 'curl', 'nc') for argument in args):
            continue
        return True
    return False


def _remote_kill(command, hosts, depth=0):
    """Recognize literal SSH destinations and their inline or heredoc payload."""
    if depth > 2 or not isinstance(command, str):
        return False
    all_tokens = _tokens(command)
    if len(all_tokens) >= 3 and all_tokens[0].rsplit('/', 1)[-1] in ('bash', 'sh'):
        if all_tokens[1].startswith('-') and 'c' in all_tokens[1]:
            return _remote_kill(all_tokens[2], hosts, depth + 1)
    lines = command.splitlines()
    for line_index, line in enumerate(lines):
        tokens = _tokens(line)
        for ssh_index, token in enumerate(tokens):
            if token.rsplit('/', 1)[-1] != 'ssh':
                continue
            # A printed or quoted example is not an executed SSH command.
            boundary = max((offset for offset in range(ssh_index) if _separator(tokens[offset])), default=-1)
            prefix = tokens[boundary + 1:ssh_index]
            if prefix and prefix[0].rsplit('/', 1)[-1] not in ('timeout', 'setsid', 'sudo', 'env'):
                continue
            for host_index in range(ssh_index + 1, len(tokens)):
                destination = tokens[host_index]
                if _separator(destination):
                    break
                host = destination.rsplit('@', 1)[-1].strip('[]')
                if host not in hosts:
                    continue
                payload = []
                tail = tokens[host_index + 1:]
                for value in tail:
                    if _separator(value) or value in ('<', '>', '<<', '<<-'):
                        break
                    payload.append(value)
                if _destructive(' '.join(payload)):
                    return True
                # Only a remote shell consumes the following heredoc as code.
                if payload and payload[0].split()[0].rsplit('/', 1)[-1] in ('bash', 'sh'):
                    for offset, value in enumerate(tail[:-1]):
                        if value not in ('<<', '<<-'):
                            continue
                        delimiter = tail[offset + 1]
                        remote_lines = []
                        for remote_line in lines[line_index + 1:]:
                            if remote_line.strip() == delimiter:
                                if _destructive('\n'.join(remote_lines)):
                                    return True
                                break
                            remote_lines.append(remote_line)
    return False


def _seqs(rows, extra=()):
    values = list(extra) + [row.get('seq') for row in rows]
    return sorted({value for value in values if isinstance(value, int) and not isinstance(value, bool)})


def explain_elimination(player, info, players, events, observed_at):
    """Return a JSON-ready explanation without mutating inputs or trusting claims.

    ``observed_at`` is the current explanation time; ``info.death_at`` anchors
    correlation when a completed tool event arrives after the first observation.
    """
    def explanation(cause, confidence, summary, attacker=None, rows=()):
        return {'cause': cause, 'confidence': confidence, 'attacker': attacker,
                'evidence_seqs': _seqs(rows, base_seqs), 'summary': summary}

    base_seqs = []
    if info.get('alive') is not False:
        return explanation('unknown', 'unknown', 'No external elimination observation is available.')
    death_at = info.get('death_at')
    death_at = death_at if _number(death_at) else observed_at
    if not _number(death_at) or not _number(observed_at):
        return explanation('unknown', 'unknown', 'Elimination timing is unavailable.')
    recent = [event for event in events if isinstance(event, dict) and _number(event.get('time'))
              and death_at - WINDOW_SECONDS <= event['time'] <= min(observed_at, death_at + COMPLETION_GRACE_SECONDS)]
    deaths = [event for event in recent if event.get('kind') == 'elimination'
              and (event.get('data') or {}).get('contestant') == player and event.get('player') == 'referee']
    base_seqs = _seqs(deaths, [info.get('observation_event_seq')])
    reason = info.get('reason') or ''
    if info.get('oom_killed') is True:
        return explanation('oom', 'observed', 'Docker reported that this container was killed by OOM.')

    externally_observed = bool(deaths or info.get('state') == 'eliminated' or reason in ('Original session exited', 'Container stopped'))
    if not externally_observed:
        return explanation('unknown', 'unknown', 'No external session or container death evidence is available.')
    hosts = {player}
    addresses = info.get('arena_ips') or []
    if isinstance(addresses, str):
        addresses = [addresses]
    hosts.update(address for address in addresses if isinstance(address, str))
    if isinstance(info.get('hostname'), str):
        hosts.add(info['hostname'])
    candidates = {}
    for event in recent:
        attacker = event.get('player')
        data = event.get('data') or {}
        if attacker == player or attacker not in players or event.get('kind') != 'tool' or data.get('status') != 'completed':
            continue
        output = data.get('output') or ''
        if not isinstance(output, str):
            continue
        success = data.get('exit_code') == 0 and not isinstance(data.get('exit_code'), bool)
        success = success or (data.get('exit_code') is None and bool(re.search(r'"alive"\s*:\s*false|\b(?:killed|terminated)\b', output, re.I)))
        if not success or re.search(r'permission denied|connection refused|no such process|operation not permitted|failed to connect|timed out', output, re.I):
            continue
        if not _remote_kill(event.get('text'), hosts):
            continue
        starts = [row for row in recent if row.get('player') == attacker and row.get('kind') == 'tool'
                  and (row.get('data') or {}).get('status') == 'running' and data.get('tool_id')
                  and (row.get('data') or {}).get('tool_id') == data.get('tool_id') and row['time'] <= event['time']]
        duration = data.get('duration_seconds')
        started_at = min(row['time'] for row in starts) if starts else event['time'] - duration if _number(duration) and duration >= 0 else event['time']
        if not death_at - WINDOW_SECONDS <= started_at <= death_at:
            continue
        candidates.setdefault(attacker, []).extend(starts + [event])
    if len(candidates) == 1:
        attacker, rows = next(iter(candidates.items()))
        name = players[attacker].get('name') or attacker
        label = {'codex': 'Codex', 'claude': 'Claude Code', 'gemini': 'Gemini',
                 'grok': 'Grok', 'compatible': 'Compatible API'}.get(players[attacker].get('harness'))
        if label:
            name = f'{name} ({label})'
        return explanation('attack', 'probable',
                           f'{name} reported a successful remote kill against this computer immediately before its observed elimination. Tool logs are untrusted; attribution is probable.',
                           attacker, rows)
    if len(candidates) > 1:
        rows = [row for evidence in candidates.values() for row in evidence]
        return explanation('unknown', 'unknown', 'Several contestants reported remote kill actions near the elimination; the attacker cannot be resolved.', rows=rows)

    failed = [event for event in recent if event.get('player') == player and event.get('kind') == 'session'
              and (event.get('data') or {}).get('phase') == 'failed']
    current_failure = info.get('error_phase') == 'failed' and _number(info.get('error_at')) and death_at - WINDOW_SECONDS <= info['error_at'] <= min(observed_at, death_at + COMPLETION_GRACE_SECONDS)
    if failed or current_failure:
        last = max(failed, key=lambda event: event['time']) if failed else None
        data = (last.get('data') or {}) if last else {}
        category = data.get('error_category') or (info.get('error_category') if current_failure else None)
        message = (last.get('text') or '') if last else (info.get('last_error') or '')
        code = data.get('exit_code') if data.get('exit_code') is not None else info.get('model_exit_code')
        failure_at = last['time'] if last else info['error_at']
        policy = [event for event in recent if event.get('player') == player and event.get('kind') == 'error'
                  and failure_at - COMPLETION_GRACE_SECONDS <= event['time'] <= failure_at
                  and re.search(r'flagged.*cybersecurity|policy.*(?:block|refus)|content.*(?:flagged|filter)', str(event.get('text', '')), re.I)]
        if category == 'model_signal' or (_number(code) and code < 0):
            signal = f' (signal {-code:g})' if _number(code) and code < 0 else ''
            return explanation('model_signal', 'reported', f'The contestant reported its model process ended by signal{signal}; the referee observed elimination.', rows=failed)
        if category in ('policy_block', 'provider_block') or (policy and category in (None, 'model_error')):
            return explanation('policy_block', 'reported', 'The contestant reported a policy block followed by a fatal session exit.', rows=failed + policy)
        if category in ('provider_error', 'provider_response', 'provider_request') or re.search(r'provider (?:request failed|returned|did not complete)|HTTP (?:4\d\d|5\d\d)', str(message), re.I):
            return explanation('provider_error', 'reported', 'The contestant reported a fatal provider request or response error; the referee observed elimination.', rows=failed)
        if category in ('startup', 'startup_error', 'configuration', 'missing_session') or re.search(r'failed to start|no conversation ID|invalid provider configuration|cannot resume reliably', str(message), re.I):
            return explanation('startup_error', 'reported', 'The contestant reported a fatal startup or conversation configuration error.', rows=failed)
        return explanation('model_error', 'reported', 'The contestant reported a fatal model or session failure; its cause is not established.', rows=failed)
    if reason == 'Container stopped' or info.get('container') in ('exited', 'dead'):
        return explanation('container_stopped', 'observed', 'The referee observed this container stop; no attacker is established.')
    if reason == 'Original session exited':
        return explanation('session_exited', 'observed', 'The referee observed the original session exit; no attacker or underlying cause is established.')
    return explanation('unknown', 'unknown', 'The contestant was eliminated; the available evidence does not establish its cause.')
