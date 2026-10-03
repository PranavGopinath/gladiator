"""Normalize public harness events; never forward reasoning payloads."""
import json
import math
import time


def count(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else 0


def tool_event(info, emit, identity, name, status, output=None, exit_code=None):
    key = f'{info.get("turn", 0)}:{identity}'
    active = info.setdefault('active_tools', {})
    started = active.get(key, {}).get('started_at')
    now = time.time()
    if status == 'running':
        if key in active:
            return
        active[key] = {'name': name, 'started_at': now}
        info['tool_count'] = info.get('tool_count', 0) + 1
        info['activity'] = 'tool'
    else:
        if started is None:
            info['tool_count'] = info.get('tool_count', 0) + 1
        active.pop(key, None)
        info['activity'] = 'tool' if active else 'active'
    info['current_tool'] = next(reversed(active.values())) if active else None
    emit('tool', name, {'tool_id': key, 'status': status, 'output': output, 'exit_code': exit_code,
                        'duration_seconds': now - started if started is not None else None})


def consume(info, harness, data, emit):
    if not isinstance(data, dict):
        return
    kind = data.get('type')
    if kind == 'arena.model':
        info.update(model=data.get('model') or info.get('configured_model'),
                    session_id=data.get('session_id') or info.get('session_id'))
        label = 'Native harness connected' if harness in ('codex', 'claude') else 'Shared tool harness connected'
        emit('system', label, {'harness': harness, 'model': info.get('model')})
        return
    if kind == 'arena.message':
        emit('message', data.get('text', ''), {})
        return
    if kind == 'arena.tool':
        status = data.get('status')
        if status in ('running', 'completed', 'failed'):
            tool_event(info, emit, data.get('tool_id', 'shell'), data.get('name', 'shell'),
                       status, data.get('output'), data.get('exit_code'))
        return
    if kind == 'arena.usage':
        usage = data.get('usage')
        if isinstance(usage, dict):
            info['usage'] = {field: count(usage.get(field)) for field in
                             ('input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_write_tokens')}
            emit('usage', 'Model usage updated', {'usage': info['usage']})
        return
    if kind == 'arena.session':
        phase = data.get('phase')
        info.update(activity=phase, turn=data.get('turn', info.get('turn', 0)))
        if phase == 'active':
            info.update(turn_started_at=data.get('started_at', time.time()), next_turn_at=None, active_tools={}, current_tool=None)
        if phase in ('idle', 'failed', 'turn_error', 'blocked'):
            info.update(last_turn_seconds=data.get('duration_seconds'), next_turn_at=data.get('next_turn_at'),
                        current_tool=None, active_tools={})
        if data.get('session_id'):
            info['session_id'] = data['session_id']
        if phase in ('failed', 'turn_error', 'blocked'):
            info['model_exit_code'] = data.get('exit_code')
            info['last_error'] = data.get('message')
            info['error_category'] = data.get('error_category')
            info['error_phase'] = phase
            info['error_at'] = time.time()
        emit('session', data.get('message') or f'Model turn {info["turn"]} started',
             {key: data.get(key) for key in ('phase', 'turn', 'exit_code', 'error_category', 'next_turn_at', 'duration_seconds')})
        return
    if harness == 'codex':
        item = data.get('item') or {}
        if not isinstance(item, dict):
            return
        item_type = item.get('type')
        if kind == 'thread.started':
            info['session_id'] = data.get('thread_id')
        elif kind == 'turn.started':
            info.setdefault('turn_started_at', time.time())
        elif kind in ('item.started', 'item.completed') and item_type in ('command_execution', 'mcp_tool_call', 'file_change', 'web_search'):
            name = item.get('command') or item.get('tool') or item.get('query') or 'File changes'
            if item_type == 'file_change':
                name = '\n'.join(f'{c.get("kind", "update")} {c.get("path", "")}' for c in item.get('changes', []) if isinstance(c, dict))
            output = item.get('aggregated_output') if item_type == 'command_execution' else item.get('result')
            if output is not None and not isinstance(output, str):
                output = json.dumps(output)
            tool_event(info, emit, item.get('id', item_type), name,
                       'running' if kind == 'item.started' else 'failed' if item.get('status') == 'failed' else 'completed',
                       output, item.get('exit_code'))
        elif kind == 'item.completed' and item_type == 'agent_message':
            emit('message', item.get('text', ''), {})
        elif kind in ('error', 'turn.failed') or (kind == 'item.completed' and item_type == 'error'):
            error = data.get('message') or data.get('error') or item.get('message')
            if isinstance(error, dict):
                error = error.get('message', 'Model error')
            info['last_error'] = error
            emit('error', error or 'Model error', {})
        elif kind == 'turn.completed':
            usage = data.get('usage')
            turn = str(info.get('turn', 0))
            seen = info.setdefault('usage_turns', [])
            if isinstance(usage, dict) and turn not in seen:
                if not isinstance(info.get('usage'), dict):
                    info['usage'] = {'input_tokens': 0, 'output_tokens': 0, 'cached_input_tokens': 0, 'cache_write_tokens': 0}
                totals = info['usage']
                for field in ('input_tokens', 'output_tokens', 'cached_input_tokens'):
                    totals[field] += count(usage.get(field))
                totals['cache_write_tokens'] += count(usage.get('cache_write_input_tokens'))
                seen.append(turn)
            emit('usage', 'Model turn completed', {'usage': info.get('usage')})
        return
    if harness != 'claude':
        return
    if kind in ('assistant', 'user'):
        message = data.get('message') or {}
        for part in message.get('content', []) if isinstance(message, dict) else []:
            if not isinstance(part, dict):
                continue
            if part.get('type') == 'text':
                emit('message', part.get('text', ''), {})
            elif part.get('type') == 'tool_use':
                name = part.get('name', '') + '\n' + json.dumps(part.get('input', {}))
                tool_event(info, emit, part.get('id', 'tool'), name, 'running')
            elif part.get('type') == 'tool_result':
                key = f'{info.get("turn", 0)}:{part.get("tool_use_id", "tool")}'
                name = info.get('active_tools', {}).get(key, {}).get('name', 'Tool result')
                content = part.get('content', '')
                if not isinstance(content, str):
                    # Keep public text; discard thinking/reasoning content blocks.
                    content = '\n'.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text') if isinstance(content, list) else ''
                tool_event(info, emit, part.get('tool_use_id', 'tool'), name,
                           'failed' if part.get('is_error') else 'completed', content)
    elif kind == 'result':
        # Claude reports cumulative session modelUsage/cost on resume. Replace
        # snapshots instead of adding them again for every observation turn.
        model_usage = data.get('modelUsage')
        if isinstance(model_usage, dict) and model_usage:
            info['usage'] = {
                'input_tokens': sum(count(u.get('inputTokens')) + count(u.get('cacheReadInputTokens')) + count(u.get('cacheCreationInputTokens')) for u in model_usage.values() if isinstance(u, dict)),
                'output_tokens': sum(count(u.get('outputTokens')) for u in model_usage.values() if isinstance(u, dict)),
                'cached_input_tokens': sum(count(u.get('cacheReadInputTokens')) for u in model_usage.values() if isinstance(u, dict)),
                'cache_write_tokens': sum(count(u.get('cacheCreationInputTokens')) for u in model_usage.values() if isinstance(u, dict)),
            }
        if 'total_cost_usd' in data:
            info['cost_usd'] = count(data['total_cost_usd'])
        if data.get('is_error'):
            info['last_error'] = data.get('result') or 'Model turn failed'
        emit('error' if data.get('is_error') else 'usage', data.get('result') if data.get('is_error') else 'Model turn completed',
             {'usage': info.get('usage'), 'cost_usd': info.get('cost_usd')})
    elif kind == 'system' and data.get('subtype') == 'init':
        info.update(model=data.get('model') or info.get('configured_model'), session_id=data.get('session_id'))
        emit('system', 'Model session connected', {})


def memory_bytes(text):
    import re
    match = re.fullmatch(r'\s*([\d.]+)\s*([KMGT]?i?B)\s*', text, re.I)
    if not match:
        return None
    unit = match[2].upper()
    power = 'BKMGT'.index(unit[0]) if unit[0] != 'B' else 0
    return round(float(match[1]) * (1024 if 'I' in unit else 1000) ** power)


def resource_sample(data):
    memory = str(data.get('MemUsage', '')).split('/')
    try:
        cpu = float(str(data.get('CPUPerc', '')).rstrip('%'))
    except ValueError:
        cpu = None
    try:
        pids = int(data.get('PIDs', ''))
    except (ValueError, TypeError):
        pids = None
    return {'sampled_at': time.time(), 'cpu_percent': cpu,
            'memory_bytes': memory_bytes(memory[0]) if memory else None,
            'memory_limit_bytes': memory_bytes(memory[1]) if len(memory) > 1 else None, 'pids': pids}
