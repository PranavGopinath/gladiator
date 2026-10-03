import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from universal_harness import ChatClient, Conversation, provider_settings, run_session, shell_result
from supervisor import agent_command, launch_options


def response(content='Public result', calls=None, **extra):
    message = {'role': 'assistant', 'content': content, **extra}
    if calls:
        message['tool_calls'] = calls
    return {'choices': [{'message': message, 'finish_reason': 'tool_calls' if calls else 'stop'}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 2}}


class ProviderTests(unittest.TestCase):
    def server(self, responses):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append({'path': self.path, 'body': body, 'authorization': self.headers.get('Authorization')})
                value = responses.pop(0) if responses else response()
                code, payload = value[:2] if isinstance(value, tuple) else (200, value)
                payload = json.dumps(payload).encode()
                self.send_response(code)
                truncated = isinstance(value, tuple) and len(value) == 3 and value[2] == 'truncate'
                self.send_header('Content-Length', str(len(payload) + (15 if truncated else 0)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}/v1', requests

    def test_provider_defaults_keys_and_shared_tool_protocol(self):
        for harness, key_name, expected_url in [
            ('gemini', 'GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'),
            ('grok', 'XAI_API_KEY', 'https://api.x.ai/v1/chat/completions')]:
            settings = provider_settings({'AGENT': harness, 'MODEL': 'selected-model', key_name: 'fixture'})
            self.assertEqual(settings['url'], expected_url)
            url, requests = self.server([response()])
            settings['url'] = url + '/chat/completions'
            with contextlib.redirect_stdout(io.StringIO()):
                Conversation(ChatClient(settings)).turn('observe')
            self.assertEqual(requests[0]['body']['model'], 'selected-model')
            self.assertEqual(requests[0]['authorization'], 'Bearer fixture')
            self.assertEqual(requests[0]['body']['tools'][0]['function']['name'], 'shell')

    def test_tool_result_and_history_survive_turns_without_reasoning_exposure(self):
        call = {'id': 'call-1', 'type': 'function',
                'function': {'name': 'shell', 'arguments': json.dumps({'command': "printf 'fixture output'"})},
                'extra_content': {'google': {'thought_signature': 'opaque-signature'}}}
        url, requests = self.server([response('Taking a move', [call], reasoning_content='private-thought'),
                                     response('Move complete'), response('Next move')])
        settings = provider_settings({'AGENT': 'compatible', 'MODEL': 'local-model', 'API_BASE_URL': url})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            conversation = Conversation(ChatClient(settings))
            conversation.turn('first turn')
            conversation.turn('second turn')
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[0]['path'], '/v1/chat/completions')
        self.assertIsNone(requests[0]['authorization'])
        self.assertEqual(requests[1]['body']['messages'][-1]['role'], 'tool')
        self.assertIn('fixture output', requests[1]['body']['messages'][-1]['content'])
        self.assertEqual(requests[1]['body']['messages'][-2]['tool_calls'][0]['extra_content'], call['extra_content'])
        self.assertIn('first turn', json.dumps(requests[2]['body']['messages']))
        self.assertNotIn('private-thought', output.getvalue())
        self.assertNotIn('opaque-signature', output.getvalue())
        self.assertEqual(conversation.usage['input_tokens'], 9)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([event['status'] for event in events if event['type'] == 'arena.tool'], ['running', 'completed'])

    def test_api_error_preserves_session_and_next_scheduled_turn_succeeds(self):
        url, requests = self.server([(401, {'error': {'message': 'sensitive secret fixture'}})])
        output = io.StringIO()
        with patch.dict(os.environ, {'AGENT': 'compatible', 'API_BASE_URL': url, 'MODEL': 'fixture'}, clear=True):
            with contextlib.redirect_stdout(output):
                code = run_session('observe', interval=0, max_turns=2)
        self.assertEqual(code, 0)
        self.assertEqual(len(requests), 2)
        self.assertNotIn('sensitive secret fixture', output.getvalue())
        self.assertIn('HTTP 401', output.getvalue())
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        error = next(event for event in events if event.get('phase') == 'turn_error')
        self.assertEqual(error['error_category'], 'provider_http')
        self.assertEqual(error['exit_code'], 401)
        self.assertIsNotNone(error['next_turn_at'])
        self.assertEqual(events[-1]['phase'], 'idle')
        self.assertIn('observe', json.dumps(requests[1]['body']['messages']))

    def test_malformed_tool_response_does_not_poison_history(self):
        malformed = response('bad response', [{'id': 'bad-call', 'function': 'malformed'}])
        url, requests = self.server([malformed, response('recovered')])
        output = io.StringIO()
        with patch.dict(os.environ, {'AGENT': 'compatible', 'API_BASE_URL': url, 'MODEL': 'fixture'}, clear=True):
            with contextlib.redirect_stdout(output):
                code = run_session('original objective', interval=0, max_turns=2)
        self.assertEqual(code, 0)
        self.assertNotIn('bad-call', json.dumps(requests[1]['body']['messages']))
        self.assertIn('original objective', json.dumps(requests[1]['body']['messages']))
        self.assertIn('turn_error', output.getvalue())

    def test_truncated_http_response_is_recoverable(self):
        url, requests = self.server([(200, response(), 'truncate'), response('recovered')])
        output = io.StringIO()
        with patch.dict(os.environ, {'AGENT': 'compatible', 'API_BASE_URL': url, 'MODEL': 'fixture'}, clear=True):
            with contextlib.redirect_stdout(output):
                self.assertEqual(run_session('objective', interval=0, max_turns=2), 0)
        self.assertEqual(len(requests), 2)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(next(event for event in events if event.get('phase') == 'turn_error')['error_category'], 'provider_transport')
        self.assertEqual(events[-1]['phase'], 'idle')

    def test_executed_tool_history_survives_later_http_failure(self):
        call = {'id': 'move-1', 'type': 'function', 'function': {
            'name': 'shell', 'arguments': json.dumps({'command': "printf 'move-result-fixture'"})}}
        url, requests = self.server([response('move', [call]), (503, {'error': {'message': 'unavailable'}}),
                                     response('recovered')])
        with patch.dict(os.environ, {'AGENT': 'compatible', 'API_BASE_URL': url, 'MODEL': 'fixture'}, clear=True):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_session('objective', interval=0, max_turns=2), 0)
        self.assertEqual(len(requests), 3)
        messages = requests[2]['body']['messages']
        results = [message for message in messages if message.get('role') == 'tool']
        self.assertEqual(len(results), 1)
        self.assertIn('move-result-fixture', results[0]['content'])

    def test_explicit_policy_responses_park_without_retry(self):
        refusal = response('Unable to participate', refusal='Provider policy refusal')
        for blocked in [refusal,
                        {'choices': [{'finish_reason': 'content_filter'}]},
                        {'promptFeedback': {'blockReason': 'SAFETY'}},
                        (403, {'error': {'code': 'policy_violation'}})]:
            url, requests = self.server([blocked])
            output = io.StringIO()
            with patch.dict(os.environ, {'AGENT': 'compatible', 'API_BASE_URL': url, 'MODEL': 'fixture'}, clear=True):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(run_session('original objective', interval=0, max_turns=2), 0)
            self.assertEqual(len(requests), 1)
            event = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(event['phase'], 'blocked')
            self.assertEqual(event['error_category'], 'policy_block')
            self.assertIsNone(event['next_turn_at'])

    def test_policy_block_keeps_real_session_process_alive_without_more_requests(self):
        url, requests = self.server([response('Public refusal', refusal='Policy refusal')])
        env = dict(os.environ, AGENT='compatible', API_BASE_URL=url, MODEL='fixture',
                   TASK='observe', TURN_INTERVAL_SECONDS='0.05')
        process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve().parents[1] / 'session.py')],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.addCleanup(process.stdout.close)
        while True:
            event = json.loads(process.stdout.readline())
            if event.get('phase') == 'blocked':
                break
        time.sleep(.2)
        self.assertIsNone(process.poll())
        self.assertEqual(len(requests), 1)
        process.kill()
        self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)

    def test_persistent_session_pid_does_not_restart_after_kill(self):
        url, requests = self.server([response('First turn complete')])
        env = dict(os.environ, AGENT='compatible', API_BASE_URL=url, MODEL='fixture',
                   TASK='observe', TURN_INTERVAL_SECONDS='60')
        process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve().parents[1] / 'session.py')],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.addCleanup(process.stdout.close)
        events = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            event = json.loads(process.stdout.readline())
            events.append(event)
            if event.get('phase') == 'idle':
                break
        self.assertEqual(events[-1].get('phase'), 'idle')
        self.assertIn(f'session PID is {process.pid}', requests[0]['body']['messages'][-1]['content'])
        process.kill()
        self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)
        self.assertEqual(len(requests), 1)

    def test_key_file_is_runtime_only_and_required_config_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / 'key'
            secret.write_text('fixture-secret\n')
            env = {'AGENT': 'gemini', 'MODEL': 'fixture', 'GEMINI_API_KEY_FILE': str(secret)}
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(launch_options()['env']['GEMINI_API_KEY'], 'fixture-secret')
                self.assertNotIn('GEMINI_API_KEY', os.environ)
                self.assertNotIn('fixture-secret', ' '.join(agent_command()))
        with self.assertRaises(ValueError):
            provider_settings({'AGENT': 'compatible', 'MODEL': 'fixture'})
        with self.assertRaises(ValueError):
            provider_settings({'AGENT': 'gemini', 'MODEL': 'fixture'})
        with self.assertRaises(ValueError):
            provider_settings({'AGENT': 'compatible', 'API_BASE_URL': 'http://localhost/v1'})

    def test_failed_command_returns_result_without_ending_model_session(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result = shell_result({'id': 'failed-command', 'function': {
                'name': 'shell', 'arguments': json.dumps({'command': 'exit 7'})}})
        self.assertEqual(json.loads(result['content'])['exit_code'], 7)


if __name__ == '__main__':
    unittest.main()
