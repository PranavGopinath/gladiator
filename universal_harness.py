"""Persistent arena shell harness for tool-capable Chat Completions endpoints.

Official compatibility contracts:
https://ai.google.dev/gemini-api/docs/openai
https://docs.x.ai/developers/model-capabilities/legacy/chat-completions
Only public messages, commands, results and usage are emitted; reasoning remains
private. Native provider fields (including Gemini tool signatures) stay in history.
"""
import json
import http.client
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

PROVIDERS = {
    'gemini': ('https://generativelanguage.googleapis.com/v1beta/openai/', 'GEMINI_API_KEY'),
    'grok': ('https://api.x.ai/v1', 'XAI_API_KEY'),
    'compatible': ('', 'COMPATIBLE_API_KEY'),
}
SHELL_TOOL = {'type': 'function', 'function': {
    'name': 'shell', 'description': 'Run a bash command on your arena computer and return output and exit code.',
    'parameters': {'type': 'object', 'properties': {
        'command': {'type': 'string'},
        'timeout_seconds': {'type': 'number', 'description': 'Command timeout, 1–60 seconds (default 20).'},
    }, 'required': ['command']},
}}


class TurnError(RuntimeError):
    def __init__(self, message, category='provider_response', exit_code=1):
        super().__init__(message)
        self.category = category
        self.exit_code = exit_code


class PolicyBlocked(TurnError):
    def __init__(self, message, exit_code=1):
        super().__init__(message, 'policy_block', exit_code)


def blocked_feedback(body):
    if not isinstance(body, dict):
        return False
    feedback = body.get('promptFeedback') or body.get('prompt_feedback') or {}
    if not isinstance(feedback, dict):
        return False
    reason = feedback.get('blockReason') or feedback.get('block_reason')
    return bool(reason and reason != 'BLOCK_REASON_UNSPECIFIED')


def emit(kind, **fields):
    print(json.dumps({'type': 'arena.' + kind, **fields}), flush=True)


def provider_settings(env=None):
    env = os.environ if env is None else env
    kind = env.get('AGENT', 'compatible')
    if kind not in PROVIDERS:
        raise ValueError('Unsupported shared harness provider')
    default_url, key_name = PROVIDERS[kind]
    base_url = env.get('API_BASE_URL') or env.get('PROVIDER_BASE_URL') or default_url
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Provider base URL must be an HTTP(S) API base without embedded credentials')
    key_file = env.get(key_name + '_FILE') or env.get('PROVIDER_API_KEY_FILE')
    api_key = Path(key_file).read_text().strip() if key_file else env.get(key_name, '')
    if kind != 'compatible' and not api_key:
        raise ValueError('Provider API key is required')
    model = env.get('MODEL', '').strip()
    if not model:
        raise ValueError('Shared harness requires an explicit model ID')
    return {'harness': kind, 'url': base_url.rstrip('/') + '/chat/completions',
            'model': model, 'api_key': api_key}


class ChatClient:
    def __init__(self, settings):
        self.settings = settings

    def complete(self, messages):
        payload = {'model': self.settings['model'], 'messages': messages,
                   'tools': [SHELL_TOOL], 'tool_choice': 'auto'}
        headers = {'Content-Type': 'application/json'}
        if self.settings['api_key']:
            headers['Authorization'] = 'Bearer ' + self.settings['api_key']
        request = urllib.request.Request(self.settings['url'], json.dumps(payload).encode(), headers)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            # Error bodies can echo prompts or secrets; never publish them.
            from session import policy_block
            try:
                details = json.loads(exc.read(65536))
                blocked = (blocked_feedback(details) or policy_block(details.get('error', {}))) if isinstance(details, dict) else False
            except (ValueError, OSError, http.client.HTTPException):
                blocked = False
            exc.close()
            if blocked:
                raise PolicyBlocked(f'Provider policy blocked this request (HTTP {exc.code})', exc.code) from None
            raise TurnError(f'Provider request failed (HTTP {exc.code})', 'provider_http', exc.code) from None
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise TurnError('Provider request failed (' + type(exc).__name__ + ')', 'provider_transport') from None
        if blocked_feedback(body):
            raise PolicyBlocked('Provider policy blocked this request')
        if not isinstance(body, dict) or not isinstance(body.get('choices'), list) or not body['choices']:
            raise TurnError('Provider returned no model response')
        choice = body['choices'][0]
        if isinstance(choice, dict) and choice.get('finish_reason') == 'content_filter' and not isinstance(choice.get('message'), dict):
            raise PolicyBlocked('Provider policy blocked this request')
        if not isinstance(choice, dict) or not isinstance(choice.get('message'), dict):
            raise TurnError('Provider returned an invalid model response')
        if choice.get('finish_reason') == 'length':
            raise TurnError('Provider did not complete the model turn')
        return body


def shell_result(call):
    identity = call.get('id') or str(uuid.uuid4())
    function = call.get('function') or {}
    try:
        args = json.loads(function.get('arguments', '{}'))
        if function.get('name') != 'shell' or not isinstance(args, dict) or not isinstance(args.get('command'), str):
            raise ValueError('Invalid shell tool arguments')
        timeout = args.get('timeout_seconds', 20)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 60:
            raise ValueError('Command timeout must be between 1 and 60 seconds')
    except (ValueError, TypeError):
        output, code = 'Invalid shell tool arguments', 2
        emit('tool', tool_id=identity, name='shell', status='failed', output=output, exit_code=code)
        return {'role': 'tool', 'tool_call_id': identity, 'content': json.dumps({'output': output, 'exit_code': code})}
    command = args['command']
    emit('tool', tool_id=identity, name=command, status='running')
    # Shell tools have their own group so a timeout can clean up descendants.
    # The contestant process remains the permanent session PID, never restarted.
    with tempfile.TemporaryFile() as capture:
        try:
            child = subprocess.Popen(['bash', '-lc', command], stdout=capture,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        except OSError:
            output, code = 'Unable to launch shell tool', 127
            emit('tool', tool_id=identity, name=command, status='failed', output=output, exit_code=code)
            return {'role': 'tool', 'tool_call_id': identity,
                    'content': json.dumps({'output': output, 'exit_code': code})}
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            code = 124
        capture.seek(0)
        output = capture.read(16000).decode('utf-8', errors='replace')
        if code == 124:
            output += '\nCommand timed out.'
    emit('tool', tool_id=identity, name=command, status='completed' if code == 0 else 'failed',
         output=output, exit_code=code)
    return {'role': 'tool', 'tool_call_id': identity,
            'content': json.dumps({'output': output, 'exit_code': code})}


class Conversation:
    def __init__(self, client):
        self.client = client
        self.messages = [{'role': 'system', 'content':
                          'You control a Linux arena contestant. Use the shell tool to observe and act. '
                          'Follow the match objective and scope in the user brief. '
                          'Report short public action summaries, without private reasoning.'}]
        self.usage = {'input_tokens': 0, 'output_tokens': 0, 'cached_input_tokens': 0, 'cache_write_tokens': 0}

    def turn(self, prompt):
        self.messages.append({'role': 'user', 'content': prompt})
        for _ in range(32):
            body = self.client.complete(self.messages)
            message = dict(body['choices'][0]['message'])
            message['role'] = 'assistant'
            content = message.get('content')
            refusal = message.get('refusal')
            blocked = body['choices'][0].get('finish_reason') == 'content_filter' or bool(refusal)
            calls = [] if blocked else message.get('tool_calls')
            if calls is None:
                calls = []
            # Validate the complete tool batch before committing it to history.
            # A malformed assistant call cannot leave an unmatched tool request
            # that poisons every subsequent turn.
            if not isinstance(calls, list) or any(
                    not isinstance(call, dict) or not isinstance(call.get('id'), str) or not call['id'] or
                    not isinstance(call.get('function'), dict) or
                    not isinstance(call['function'].get('name'), str) or
                    not isinstance(call['function'].get('arguments'), str)
                    for call in calls):
                raise TurnError('Provider returned invalid tool calls')
            if len({call['id'] for call in calls}) != len(calls):
                raise TurnError('Provider returned duplicate tool call IDs')
            if not calls and content is None and not blocked:
                raise TurnError('Provider returned no public message or tool calls')
            if content is not None and not isinstance(content, (str, list)):
                raise TurnError('Provider returned invalid message content')
            if blocked:
                # Preserve a public refusal but do not retain/execute any calls
                # in a blocked completion or resubmit the request.
                message.pop('tool_calls', None)
            self.messages.append(message)
            usage = body.get('usage') or {}
            if isinstance(usage, dict):
                details = usage.get('prompt_tokens_details') or {}
                for field, source in [('input_tokens', usage.get('prompt_tokens')),
                                      ('output_tokens', usage.get('completion_tokens')),
                                      ('cached_input_tokens', details.get('cached_tokens') if isinstance(details, dict) else 0)]:
                    if isinstance(source, int) and not isinstance(source, bool) and source >= 0:
                        self.usage[field] += source
                emit('usage', usage=dict(self.usage))
            if isinstance(content, str) and content:
                emit('message', text=content)
            if isinstance(refusal, str) and refusal:
                emit('message', text=refusal)
            if blocked:
                raise PolicyBlocked('Provider policy blocked the model turn')
            if not calls:
                return
            for call in calls:
                try:
                    result = shell_result(call)
                except (OSError, ValueError):
                    # Keep one result per committed call even if local tool
                    # setup fails, so the next request has a valid history.
                    result = {'role': 'tool', 'tool_call_id': call['id'], 'content':
                              json.dumps({'output': 'Tool execution could not complete', 'exit_code': 127})}
                    emit('tool', tool_id=call['id'], name=call['function']['name'],
                         status='failed', output='Tool execution could not complete', exit_code=127)
                self.messages.append(result)
        raise TurnError('Model exceeded the per-turn tool request limit', 'tool_limit')


def run_session(task, interval=15, max_turns=0):
    from session import FOLLOWUP, park
    session_id = str(uuid.uuid4())
    turn = 0
    started_session = time.monotonic()
    os.environ['ARENA_SESSION_PID'] = str(os.getpid())
    try:
        settings = provider_settings()
        emit('model', harness=settings['harness'], model=settings['model'], session_id=session_id)
        conversation = Conversation(ChatClient(settings))
        while True:
            turn += 1
            started = time.time()
            prompt = (task + f'\nYour persistent contestant session PID is {os.getpid()}. '
                      'Normal replies do not end this session; a new turn follows automatically.' if turn == 1
                      else f'Turn {turn}; approximately {int(time.monotonic() - started_session)} seconds elapsed. ' + FOLLOWUP)
            emit('session', phase='active', turn=turn, started_at=started, session_id=session_id)
            try:
                conversation.turn(prompt)
            except PolicyBlocked as exc:
                emit('session', phase='blocked', turn=turn, session_id=session_id,
                     exit_code=exc.exit_code, error_category=exc.category,
                     duration_seconds=time.time() - started, next_turn_at=None,
                     message=str(exc) + '. Session remains alive; further model calls are parked.')
                return park(interval, max_turns)
            except (TurnError, OSError, ValueError) as exc:
                emit('session', phase='turn_error', turn=turn, session_id=session_id,
                     exit_code=exc.exit_code if isinstance(exc, TurnError) else 1,
                     error_category=exc.category if isinstance(exc, TurnError) else 'tool_error',
                     duration_seconds=time.time() - started, next_turn_at=time.time() + interval,
                     message=(str(exc) if isinstance(exc, TurnError) else 'Tool execution could not complete') +
                             '. Session remains alive; next scheduled turn retains the conversation.')
                if max_turns and turn >= max_turns:
                    return 0
                time.sleep(interval)
                continue
            emit('session', phase='idle', turn=turn, session_id=session_id,
                 duration_seconds=time.time() - started, next_turn_at=time.time() + interval,
                 message=f'Turn complete. Session remains alive; next observation in {interval:g}s.')
            if max_turns and turn >= max_turns:
                return 0
            time.sleep(interval)
    except (OSError, ValueError, RuntimeError) as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else 'Invalid provider configuration or tool execution'
        emit('session', phase='failed', turn=turn, session_id=session_id, exit_code=1,
             error_category='configuration',
             message=message + '; contestant will not restart.')
        return 1


if __name__ == '__main__':
    sys.exit(run_session(sys.argv[1] if len(sys.argv) > 1 else os.environ.get('TASK', ''),
                         float(os.environ.get('TURN_INTERVAL_SECONDS', '15')),
                         int(os.environ.get('SESSION_TEST_TURNS', '0'))))
