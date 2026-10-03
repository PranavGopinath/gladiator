import unittest
from arena_telemetry import consume


class TelemetryTests(unittest.TestCase):
    def test_turn_errors_and_blocks_keep_liveness_and_error_metadata(self):
        info = {'alive': True, 'state': 'standing'}
        events = []
        for phase in ('turn_error', 'blocked'):
            consume(info, 'codex', {'type': 'arena.session', 'phase': phase, 'turn': 2,
                    'message': 'Turn ended', 'exit_code': 1,
                    'error_category': 'policy_block' if phase == 'blocked' else 'cli_exit',
                    'next_turn_at': None if phase == 'blocked' else 123},
                    lambda *args: events.append(args))
            self.assertTrue(info['alive'])
            self.assertEqual(info['state'], 'standing')
            self.assertEqual(info['activity'], phase)
            self.assertEqual(events[-1][2]['error_category'], info['error_category'])
        self.assertIsNone(info['next_turn_at'])

    def test_codex_usage_initial_null_and_duplicate_turn(self):
        info = {'usage': None, 'turn': 1}
        event = {'type': 'turn.completed', 'usage': {'input_tokens': 12, 'output_tokens': 3}}
        consume(info, 'codex', event, lambda *args: None)
        consume(info, 'codex', event, lambda *args: None)
        self.assertEqual(info['usage']['input_tokens'], 12)
        self.assertEqual(info['usage']['output_tokens'], 3)

    def test_shared_tool_lifecycle_and_cumulative_usage(self):
        info = {'turn': 1}
        events = []
        emit = lambda *args: events.append(args)
        consume(info, 'gemini', {'type': 'arena.model', 'model': 'fixture', 'session_id': 'test'}, emit)
        consume(info, 'gemini', {'type': 'arena.tool', 'tool_id': '1', 'name': 'pwd', 'status': 'running'}, emit)
        self.assertEqual(info['activity'], 'tool')
        consume(info, 'gemini', {'type': 'arena.tool', 'tool_id': '1', 'name': 'pwd', 'status': 'completed', 'exit_code': 0, 'output': '/workspace'}, emit)
        self.assertEqual(info['tool_count'], 1)
        self.assertIsNone(info['current_tool'])
        for n in (8, 12):
            consume(info, 'gemini', {'type': 'arena.usage', 'usage': {'input_tokens': n}}, emit)
        self.assertEqual(info['usage']['input_tokens'], 12)
        self.assertEqual(info['model'], 'fixture')

    def test_claude_cost_and_usage_are_not_double_counted(self):
        info = {}
        data = {'type': 'result', 'total_cost_usd': .25,
                'modelUsage': {'example': {'inputTokens': 10, 'cacheReadInputTokens': 20, 'outputTokens': 5}}}
        for _ in range(2):
            consume(info, 'claude', data, lambda *args: None)
        self.assertEqual(info['cost_usd'], .25)
        self.assertEqual(info['usage']['input_tokens'], 30)

    def test_private_reasoning_is_ignored(self):
        events = []
        consume({}, 'compatible', {'type': 'reasoning', 'text': 'private'}, lambda *e: events.append(e))
        consume({}, 'claude', {'type': 'assistant', 'message': {'content': [{'type': 'thinking', 'thinking': 'private'}]}}, lambda *e: events.append(e))
        self.assertEqual(events, [])


if __name__ == '__main__':
    unittest.main()
