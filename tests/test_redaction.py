import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard


class RedactionTests(unittest.TestCase):
    def test_arbitrary_provider_and_oauth_values_are_redacted_from_nested_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secrets = root / '.secrets'
            secrets.mkdir()
            (secrets / 'compatible-api-key').write_text('provider-password-fixture\n')
            (secrets / 'gemini-api-key').write_text('opaque-google-fixture\n')
            (secrets / 'codex-auth.json').write_text(json.dumps({
                'auth_mode': 'chatgpt', 'tokens': {'access_token': 'opaque-access-fixture',
                                                'refresh_token': 'opaque-refresh-fixture',
                                                'id_token': 'opaque-id-fixture'},
                'OPENAI_API_KEY': 'opaque-openai-fixture',
            }))
            with patch.object(dashboard, 'ROOT', root):
                payload = dashboard.sanitized({'text': 'provider-password-fixture', 'data': {
                    'output': 'opaque-access-fixture opaque-refresh-fixture opaque-id-fixture',
                    'other': ['opaque-google-fixture', 'opaque-openai-fixture', 'chatgpt']}})
            text = json.dumps(payload)
            self.assertNotIn('opaque-', text)
            self.assertNotIn('provider-password-fixture', text)
            self.assertIn('chatgpt', text)
            self.assertIn('credential redacted', text)

    def test_cache_avoids_rereading_and_refreshes_on_rotation_or_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secrets = root / '.secrets'
            secrets.mkdir()
            key = secrets / 'compatible-api-key'
            key.write_text('original-fixture-credential')
            with patch.object(dashboard, 'ROOT', root):
                self.assertNotIn('original-fixture-credential', dashboard.clean('original-fixture-credential'))
                with patch.object(Path, 'read_text', side_effect=AssertionError('Cache unnecessarily reread a secret')):
                    self.assertNotIn('original-fixture-credential', dashboard.clean('original-fixture-credential'))
                replacement = secrets / 'replacement'
                replacement.write_text('rotated-fixture-credential')
                replacement.replace(key)
                self.assertNotIn('rotated-fixture-credential', dashboard.clean('rotated-fixture-credential'))
                key.unlink()
                self.assertEqual(dashboard.clean('rotated-fixture-credential'), 'rotated-fixture-credential')

    def test_redaction_happens_before_public_length_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.secrets').mkdir()
            (root / '.secrets/compatible-api-key').write_text('fixture-cross-boundary-secret')
            with patch.object(dashboard, 'ROOT', root):
                text = dashboard.clean('a' * 31995 + 'fixture-cross-boundary-secret')
            self.assertEqual(len(text), 32000)
            self.assertNotIn('fixtu', text)


if __name__ == '__main__':
    unittest.main()
