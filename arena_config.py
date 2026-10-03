"""Validated match settings and Compose generation for independent contestants."""
import math
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def default_config():
    return {'duration_seconds': 300, 'turn_interval_seconds': 15,
            'prompt': (ROOT / 'arena-prompt.txt').read_text(),
            'players': [{'name': 'Codex', 'harness': 'codex', 'model': os.getenv('CODEX_MODEL', '')},
                        {'name': 'Claude', 'harness': 'claude', 'model': os.getenv('CLAUDE_MODEL', 'sonnet')}]}


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError('Match settings must be an object')
    allowed = {'players', 'duration_seconds', 'turn_interval_seconds', 'prompt'}
    if set(value) - allowed:
        raise ValueError('Unknown match setting')
    settings = default_config()
    settings.update(value)
    for key, low, high in [('duration_seconds', 10, 3600), ('turn_interval_seconds', 1, 300)]:
        number = settings[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f'{key} must be between {low} and {high}')
    prompt = settings['prompt']
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        raise ValueError('Prompt must contain 1–32,000 characters')
    players = settings['players']
    if not isinstance(players, list) or not 2 <= len(players) <= 4:
        raise ValueError('Choose between two and four contestants')
    normalized = []
    for index, player in enumerate(players):
        if not isinstance(player, dict) or set(player) - {'id', 'name', 'harness', 'model'}:
            raise ValueError('Invalid contestant settings')
        harness, name, model = player.get('harness'), player.get('name'), player.get('model', '')
        if harness not in ('codex', 'claude'):
            raise ValueError('Harness must be codex or claude')
        if not isinstance(name, str) or not name.strip() or len(name) > 48 or any(ord(c) < 32 for c in name):
            raise ValueError('Contestant names must contain 1–48 printable characters')
        if not isinstance(model, str) or len(model) > 160 or any(c.isspace() for c in model.strip()):
            raise ValueError('Model must be a model ID, or empty for the harness default')
        normalized.append({'id': f'agent-{index + 1}', 'name': name.strip(), 'harness': harness, 'model': model.strip()})
    if len({p['name'].casefold() for p in normalized}) != len(normalized):
        raise ValueError('Give each contestant a different name')
    settings['players'] = normalized
    return settings


def compose_config(settings):
    config = {'services': {}, 'networks': {'arena': {'driver': 'bridge'}}, 'secrets': {}, 'volumes': {}}
    addresses = ', '.join(p['id'] for p in settings['players'])
    for player in settings['players']:
        identity, harness = player['id'], player['harness']
        prompt = settings['prompt'].replace('{SELF}', identity).replace('{CONTESTANT_ADDRESSES}', addresses)
        prompt += f'\nYour display name is {player["name"]}. Your arena address is {identity}.'
        service = {'extends': {'file': str(ROOT / 'compose.yaml'), 'service': 'agent'},
                   'profiles': [], 'environment': {
                       'AGENT': harness, 'MODEL': player['model'], 'CODEX_API_KEY': '',
                       'WAIT_FOR_START': '1', 'CONTINUOUS_SESSION': '1', 'CLAUDE_MAX_TURNS': '0',
                       'TURN_INTERVAL_SECONDS': str(settings['turn_interval_seconds']), 'TASK': prompt}}
        if harness == 'codex':
            service['environment']['CODEX_AUTH_FILE'] = '/run/secrets/codex_auth'
            service['secrets'] = ['codex_auth']
            volume = f'{identity}-codex-home'
            service['volumes'] = [f'{volume}:/root/.codex']
            config['volumes'][volume] = {'name': f'agent-arena-{volume}'}
            config['secrets']['codex_auth'] = {'file': str(ROOT / '.secrets/codex-auth.json')}
        else:
            service['environment']['ANTHROPIC_API_KEY_FILE'] = '/run/secrets/anthropic_api_key'
            service['secrets'] = ['anthropic_api_key']
            config['secrets']['anthropic_api_key'] = {'file': str(ROOT / '.secrets/anthropic-api-key')}
        config['services'][identity] = service
    return config


def check_credentials(settings):
    required = {'codex': '.secrets/codex-auth.json', 'claude': '.secrets/anthropic-api-key'}
    missing = [required[h] for h in sorted({p['harness'] for p in settings['players']}) if not (ROOT / required[h]).is_file()]
    if missing:
        raise ValueError('Missing credential files: ' + ', '.join(missing) + '. Use the existing credential setup in README.md.')
