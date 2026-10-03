import tempfile
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import arena_config


def player(harness='codex', index=1, **overrides):
    value = {'name': f'Gladiator {index}', 'harness': harness, 'model': 'test-model'}
    value.update(overrides)
    return value


class ArenaConfigTests(unittest.TestCase):
    def settings(self, players, **overrides):
        value = dict(prompt='You are {SELF}. Peers {CONTESTANT_ADDRESSES}.', players=players)
        value.update(overrides)
        return arena_config.validate_config(value)

    def test_two_to_five_contestants_and_repeated_providers(self):
        for count in range(2, 6):
            settings = self.settings([player(index=i, model=f'model-{i}') for i in range(count)])
            self.assertEqual([p['id'] for p in settings['players']], [f'agent-{i + 1}' for i in range(count)])
            self.assertEqual([p['model'] for p in settings['players']], [f'model-{i}' for i in range(count)])
        for count in (0, 1, 6):
            with self.assertRaisesRegex(ValueError, 'two and five'):
                self.settings([player(index=i) for i in range(count)])

    def test_each_provider_accepts_independent_model(self):
        contestants = [player(harness, i, model=f'vendor/model-{i}:latest')
                       for i, harness in enumerate(arena_config.PROVIDERS)]
        contestants[-1]['base_url'] = 'http://host.docker.internal:11434/v1/'
        settings = self.settings(contestants)
        self.assertEqual(settings['players'][-1]['base_url'], 'http://host.docker.internal:11434/v1')
        self.assertEqual(len(settings['players']), 5)
        for harness in ('gemini', 'grok', 'compatible'):
            with self.assertRaisesRegex(ValueError, 'explicit model'):
                self.settings([player(harness, model=''), player(index=2)])

    def test_invalid_models_and_harnesses_are_rejected(self):
        for model in ([], True, '-x', 'model;touch', 'model\ncommand', '$(command)', 'x' * 161):
            with self.subTest(model=model), self.assertRaises(ValueError):
                self.settings([player(model=model), player(index=2)])
        for harness in ('unknown', [], None):
            with self.subTest(harness=harness), self.assertRaises(ValueError):
                self.settings([player(harness=harness), player(index=2)])

    def test_ids_are_generated_and_names_are_unique_printable(self):
        settings = self.settings([player(id='$(injected)'), player(index=2)])
        self.assertEqual(settings['players'][0]['id'], 'agent-1')
        for name in ('Gladiator 2', 'gladiator 2', 'name\x7f', 'name\ncommand'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.settings([player(name=name), player(index=2)])

    def test_endpoints_require_http_without_embedded_credentials(self):
        for endpoint in (None, '', 'file:///secret', 'http://', 'http://user:pass@example.com/v1',
                         'https://example.com/v1?key=private', 'http://localhost:99999',
                         'http://localhost/\ncmd', 'http://localhost\\@example.com', 'https://example.com/#fragment'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                self.settings([player('compatible', base_url=endpoint), player(index=2)])
        with self.assertRaisesRegex(ValueError, 'Only compatible'):
            self.settings([player('gemini', base_url='https://example.com/v1'), player(index=2)])

    def test_compose_mounts_only_selected_credentials_and_scopes_volumes(self):
        contestants = [player(harness, i) for i, harness in enumerate(arena_config.PROVIDERS)]
        contestants[-1]['base_url'] = 'http://host.docker.internal:11434/v1'
        settings = self.settings(contestants)
        with tempfile.TemporaryDirectory() as directory, patch.object(arena_config, 'ROOT', Path(directory)):
            compose = arena_config.compose_config(settings)
        self.assertEqual(compose['volumes'], {'agent-1-codex-home': {}})
        for contestant in settings['players'][:-1]:
            provider = arena_config.PROVIDERS[contestant['harness']]
            service = compose['services'][contestant['id']]
            self.assertEqual(service['secrets'], [provider['secret']])
            self.assertEqual(service['environment'][provider['credential_env']], f'/run/secrets/{provider["secret"]}')
            self.assertEqual(service['environment']['MODEL'], contestant['model'])
            self.assertEqual(service['environment']['AGENT_COMMAND'], '')
            foreign_vars = {entry['credential_env'] for key, entry in arena_config.PROVIDERS.items()
                            if key != contestant['harness']}
            self.assertFalse(foreign_vars.intersection(service['environment']))
        compatible = compose['services']['agent-5']
        self.assertNotIn('secrets', compatible)
        self.assertEqual(compatible['environment']['API_BASE_URL'], contestants[-1]['base_url'])
        self.assertIn('host.docker.internal:host-gateway', compatible['extra_hosts'])

    def test_check_credentials_is_provider_specific_and_local_key_optional(self):
        gemini = self.settings([player('gemini'), player('grok', index=2)])
        with tempfile.TemporaryDirectory() as directory, patch.object(arena_config, 'ROOT', Path(directory)):
            (Path(directory) / 'arena-prompt.txt').write_text('Test arena prompt')
            secrets = Path(directory) / '.secrets'
            secrets.mkdir()
            (secrets / 'gemini-api-key').touch()
            with self.assertRaisesRegex(ValueError, 'grok: .secrets/xai-api-key') as error:
                arena_config.check_credentials(gemini)
            self.assertNotIn('gemini:', str(error.exception))
            (secrets / 'xai-api-key').touch()
            arena_config.check_credentials(gemini)
            for endpoint in ('http://host.docker.internal:11434/v1', 'http://127.0.0.1:8000/v1', 'http://[::1]:8000/v1'):
                local = self.settings([player('compatible', base_url=endpoint), player('grok', index=2)])
                arena_config.check_credentials(local)
            remote = self.settings([player('compatible', base_url='https://models.example/v1'), player('grok', index=2)])
            with self.assertRaisesRegex(ValueError, 'compatible: .secrets/compatible-api-key'):
                arena_config.check_credentials(remote)
            (secrets / 'compatible-api-key').touch()
            arena_config.check_credentials(remote)
            self.assertEqual(arena_config.compose_config(remote)['services']['agent-1']['secrets'], ['compatible_api_key'])

    def test_prompt_has_real_match_bound_and_all_peers(self):
        settings = self.settings([player(), player(index=2)], duration_seconds=90,
                                 prompt='{SELF}: {CONTESTANT_ADDRESSES}. {DURATION_SECONDS} seconds, '
                                        '{DURATION_MINUTES} minutes, {MATCH_DURATION}. The match lasts five minutes.')
        prompt = arena_config.compose_config(settings)['services']['agent-2']['environment']['TASK']
        self.assertIn('agent-2: agent-1, agent-2.', prompt)
        self.assertIn('90 seconds, 1.5 minutes, 90 seconds', prompt)
        self.assertIn('The match lasts 90 seconds.', prompt)
        self.assertNotIn('five minutes', prompt)
        self.assertIn('deadline is 90 seconds', prompt)

    def test_compose_preserves_literal_dollars_in_prompt(self):
        settings = self.settings([player(), player(index=2)], prompt='Inspect $HOME and ${API_KEY}; report $(command).')
        prompt = arena_config.compose_config(settings)['services']['agent-1']['environment']['TASK']
        self.assertIn('Inspect $$HOME and $${API_KEY}; report $$(command).', prompt)


if __name__ == '__main__':
    unittest.main()
