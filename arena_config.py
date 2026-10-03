"""Validated match settings and Compose generation for independent contestants."""
import ipaddress
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent

# This registry is shared by configuration validation, the dashboard, and Compose.
# Model IDs are selected per contestant rather than implied by a provider.
PROVIDERS = {
    'codex': {'label': 'Codex / OpenAI', 'credential_file': '.secrets/codex-auth.json',
              'secret': 'codex_auth', 'credential_env': 'CODEX_AUTH_FILE', 'model_required': False},
    'claude': {'label': 'Claude Code / Anthropic', 'credential_file': '.secrets/anthropic-api-key',
               'secret': 'anthropic_api_key', 'credential_env': 'ANTHROPIC_API_KEY_FILE', 'model_required': False},
    'gemini': {'label': 'Gemini', 'credential_file': '.secrets/gemini-api-key',
               'secret': 'gemini_api_key', 'credential_env': 'GEMINI_API_KEY_FILE', 'model_required': True,
               'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai/'},
    'grok': {'label': 'Grok / xAI', 'credential_file': '.secrets/xai-api-key',
             'secret': 'xai_api_key', 'credential_env': 'XAI_API_KEY_FILE', 'model_required': True,
             'base_url': 'https://api.x.ai/v1'},
    'compatible': {'label': 'OpenAI compatible', 'credential_file': '.secrets/compatible-api-key',
                   'secret': 'compatible_api_key', 'credential_env': 'COMPATIBLE_API_KEY_FILE',
                   'model_required': True},
}


def validate_base_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError('Compatible API base_url must be an http or https URL')
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname and
                 parsed.username is None and parsed.password is None and
                 not parsed.query and not parsed.fragment and '\\' not in value)
        parsed.port  # Validate malformed and out-of-range ports without opening the endpoint.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Compatible API base_url must be an http or https URL without credentials, query, or fragment')
    return value.rstrip('/')


def local_endpoint(base_url):
    """Recognize explicitly local services that can operate without an API key."""
    host = urlsplit(base_url).hostname
    if host in ('localhost', 'host.docker.internal', 'gateway.docker.internal'):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    local_networks = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7')
    return address.is_loopback or any(address in ipaddress.ip_network(network) for network in local_networks)


def default_config():
    return {'duration_seconds': 300, 'turn_interval_seconds': 15,
            'prompt': (ROOT / 'arena-prompt.txt').read_text(),
            'players': [{'name': 'Codex', 'harness': 'codex', 'model': os.getenv('CODEX_MODEL', 'gpt-5.5')},
                        {'name': 'Claude', 'harness': 'claude', 'model': os.getenv('CLAUDE_MODEL', 'claude-opus-4-8')}]}


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
    if not isinstance(players, list) or not 2 <= len(players) <= 5:
        raise ValueError('Choose between two and five contestants')
    normalized = []
    for index, player in enumerate(players):
        if not isinstance(player, dict) or set(player) - {'id', 'name', 'harness', 'model', 'base_url'}:
            raise ValueError('Invalid contestant settings')
        harness, name, model = player.get('harness'), player.get('name'), player.get('model', '')
        if not isinstance(harness, str) or harness not in PROVIDERS:
            raise ValueError('Harness must be codex, claude, gemini, grok, or compatible')
        if not isinstance(name, str) or not name.strip() or len(name) > 48 or not name.isprintable():
            raise ValueError('Contestant names must contain 1–48 printable characters')
        if not isinstance(model, str) or len(model) > 160 or (model.strip() and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@+,\-]*', model.strip())):
            raise ValueError('Model must be a model ID, or empty for the harness default')
        if PROVIDERS[harness]['model_required'] and not model.strip():
            raise ValueError(f'{harness} contestants require an explicit model ID')
        contestant = {'id': f'agent-{index + 1}', 'name': name.strip(), 'harness': harness, 'model': model.strip()}
        if harness == 'compatible':
            contestant['base_url'] = validate_base_url(player.get('base_url'))
        elif player.get('base_url') not in (None, ''):
            raise ValueError('Only compatible contestants accept a custom base_url')
        normalized.append(contestant)
    if len({p['name'].casefold() for p in normalized}) != len(normalized):
        raise ValueError('Give each contestant a different name')
    settings['players'] = normalized
    return settings


def compose_config(settings):
    config = {'services': {}, 'networks': {'arena': {'driver': 'bridge'}}, 'secrets': {}, 'volumes': {}}
    addresses = ', '.join(p['id'] for p in settings['players'])
    for player in settings['players']:
        identity, harness = player['id'], player['harness']
        provider = PROVIDERS[harness]
        seconds = f'{settings["duration_seconds"]:g}'
        replacements = {'{SELF}': identity, '{CONTESTANT_ADDRESSES}': addresses,
                        '{DURATION_SECONDS}': seconds,
                        '{DURATION_MINUTES}': f'{settings["duration_seconds"] / 60:g}',
                        '{MATCH_DURATION}': f'{seconds} seconds'}
        prompt = settings['prompt']
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)
        prompt = prompt.replace('The match lasts five minutes.', f'The match lasts {seconds} seconds.')
        prompt = prompt.replace('active model process fails or is killed, or your container stops.',
                                'active model process is killed by a signal, or your container stops.')
        prompt += ('\nEngine lifecycle rule: provider errors and normal nonzero CLI exits end only the turn. '
                   'Explicit policy blocks leave the session alive but paused without retrying the blocked request. '
                   'The original session process or container dying remains permanent elimination.')
        prompt += f'\nThe configured match deadline is {seconds} seconds from the start signal.'
        prompt += f'\nYour display name is {player["name"]}. Your arena address is {identity}.'
        service = {'extends': {'file': str(ROOT / 'compose.yaml'), 'service': 'agent'},
                   'profiles': [], 'environment': {
                       'AGENT': harness, 'AGENT_COMMAND': '', 'MODEL': player['model'], 'CODEX_API_KEY': '',
                       'WAIT_FOR_START': '1', 'CONTINUOUS_SESSION': '1', 'CLAUDE_MAX_TURNS': '0',
                       'TURN_INTERVAL_SECONDS': str(settings['turn_interval_seconds']), 'TASK': prompt}}
        if 'base_url' in provider or harness == 'compatible':
            service['environment']['API_BASE_URL'] = player.get('base_url', provider.get('base_url', ''))
        credential_present = (ROOT / provider['credential_file']).is_file()
        if harness != 'compatible' or credential_present:
            service['environment'][provider['credential_env']] = f'/run/secrets/{provider["secret"]}'
            service['secrets'] = [provider['secret']]
            config['secrets'][provider['secret']] = {'file': str(ROOT / provider['credential_file'])}
        if harness == 'codex':
            volume = f'{identity}-codex-home'
            service['volumes'] = [f'{volume}:/root/.codex']
            config['volumes'][volume] = {}
        if harness == 'compatible' and urlsplit(player['base_url']).hostname in ('host.docker.internal', 'gateway.docker.internal'):
            service['extra_hosts'] = ['host.docker.internal:host-gateway', 'gateway.docker.internal:host-gateway']
        # Compose interpolates environment strings even in JSON input. Preserve
        # literal prompts/URLs and prevent them from reading operator variables.
        service['environment'] = {key: value.replace('$', '$$') for key, value in service['environment'].items()}
        config['services'][identity] = service
    return config


def check_credentials(settings):
    required = {p['harness'] for p in settings['players']
                if p['harness'] != 'compatible' or not local_endpoint(p['base_url'])}
    missing = [f'{h}: {PROVIDERS[h]["credential_file"]}' for h in sorted(required)
               if not (ROOT / PROVIDERS[h]['credential_file']).is_file()]
    if missing:
        raise ValueError('Missing credential files: ' + ', '.join(missing) + '. Use the existing credential setup in README.md.')
