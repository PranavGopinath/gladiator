"""Stripe credit top-ups for the arena betting market.

Buying credits is a real Stripe payment; credits themselves are an entertainment
currency and are never paid back out as cash. Credit is granted only from the
signed `checkout.session.completed` webhook (never the browser redirect, which a
client can skip or replay), keyed on the Stripe event id so retries are no-ops.

No third-party SDK: the Stripe REST API is called over urllib and the webhook
signature is verified with stdlib hmac, matching the rest of this codebase. Keys
are read from the host `.env` at call time (like jev.py) and never enter a
contestant container. Stripe is optional — with no key configured `configured()`
is False and, in an explicitly enabled dev mode, an unsigned local top-up keeps
the market runnable without an account.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

ENV_PATH = Path(__file__).resolve().parent / '.env'
API_BASE = 'https://api.stripe.com/v1'
# 1 cent purchased == 1 credit, so a $5.00 Checkout grants 500 credits.
CREDITS_PER_CENT = 1
PACKS = {'small': 500, 'medium': 1500, 'large': 5000}  # cents
_urlopen = urllib.request.urlopen  # Indirected so tests can substitute a fake.


class PaymentError(Exception):
    pass


def _from_env(name):
    """Runtime lookup: the process environment first, then the host .env file.
    Never loads the value into os.environ, so it cannot leak into child processes
    or contestant containers."""
    value = os.environ.get(name)
    if value:
        return value.strip()
    try:
        with ENV_PATH.open() as source:
            for line in source:
                match = re.match(rf'^\s*(?:export\s+)?{re.escape(name)}\s*=\s*(.*)$', line)
                if not match:
                    continue
                raw = match[1].strip()
                if raw[:1] in ('"', "'"):
                    end = raw.find(raw[0], 1)
                    return raw[1:end].strip() if end != -1 else ''
                return re.split(r'\s+#', raw, maxsplit=1)[0].strip()
    except OSError:
        pass
    return ''


def _secret_key():
    return _from_env('STRIPE_SECRET_KEY')


def configured():
    return bool(_secret_key())


def sandbox():
    """True when the configured key is a Stripe test-mode key."""
    return '_test_' in _secret_key()[:12]  # sk_test_, rk_test_, rkcs_test_ ...


def dev_mode():
    """Unsigned local top-ups, for running the market without Stripe. Never on
    once a real secret key is present."""
    return os.environ.get('ARENA_DEV_CREDITS') == '1' and not _secret_key()


def _request(path, fields, key):
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(f'{API_BASE}/{path}', data=data, method='POST',
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with _urlopen(request, timeout=15) as response:
            return json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as error:
        try:
            message = json.loads(error.read()).get('error', {}).get('message', '')
        except (ValueError, OSError):
            message = ''
        error.close()
        # Never surface the key or raw error body.
        raise PaymentError(f'Stripe request failed ({error.code}){": " + message if message else ""}') from None
    except (OSError, ValueError):
        raise PaymentError('Stripe is unreachable or returned an invalid response') from None


def create_checkout(user, pack, success_url, cancel_url):
    key = _secret_key()
    if not key:
        raise PaymentError('Stripe is not configured')
    if pack not in PACKS:
        raise PaymentError('Unknown credit pack')
    credits = PACKS[pack] * CREDITS_PER_CENT
    fields = {
        'mode': 'payment',
        'success_url': success_url,
        'cancel_url': cancel_url,
        'customer_creation': 'always',  # Persist credits to a real Stripe Customer.
        'client_reference_id': str(user),
        'metadata[user]': str(user),
        'metadata[credits]': str(credits),
        'line_items[0][quantity]': '1',
        'line_items[0][price_data][currency]': 'usd',
        'line_items[0][price_data][unit_amount]': str(PACKS[pack]),
        'line_items[0][price_data][product_data][name]': f'Arena credits ({credits})',
    }
    session = _request('checkout/sessions', fields, key)
    if not session.get('url'):
        raise PaymentError('Stripe did not return a checkout URL')
    return {'id': session.get('id'), 'url': session['url']}


def verify_signature(body, header, secret, tolerance=300):
    """Verify a Stripe-Signature header (`t=…,v1=…`) with stdlib hmac. Returns the
    decoded event, or raises PaymentError on any mismatch or staleness."""
    parts = dict(piece.split('=', 1) for piece in header.split(',') if '=' in piece)
    timestamp, signature = parts.get('t'), parts.get('v1')
    if not timestamp or not signature:
        raise PaymentError('Malformed Stripe signature header')
    payload = body if isinstance(body, bytes) else body.encode()
    signed = f'{timestamp}.'.encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise PaymentError('Invalid Stripe signature')
    try:
        if tolerance and abs(time.time() - int(timestamp)) > tolerance:
            raise PaymentError('Stripe signature timestamp is outside the tolerance window')
    except ValueError:
        raise PaymentError('Invalid Stripe signature timestamp') from None
    try:
        return json.loads(payload)
    except ValueError:
        raise PaymentError('Invalid Stripe event payload') from None


def _grant(ledger, user, credits, key):
    if not user or credits <= 0:
        raise PaymentError('Missing user or credit amount')
    ledger.post(user, int(credits), 'purchase', key=key)
    return {'user': user, 'credits': int(credits)}


def handle_webhook(body, signature, ledger):
    """Verify a Stripe webhook and grant credits. Returns a small summary dict."""
    secret = _from_env('STRIPE_WEBHOOK_SECRET')
    if dev_mode() and not secret:
        # Local, unsigned: body is {"user": ..., "credits": ..., "id": ...}.
        payload = json.loads(body)
        return _grant(ledger, payload.get('user'), int(payload.get('credits', 0)),
                      key='dev:' + str(payload.get('id', payload.get('user'))))
    if not secret:
        raise PaymentError('Stripe webhooks are not configured (set STRIPE_WEBHOOK_SECRET)')
    event = verify_signature(body, signature, secret)
    if event.get('type') != 'checkout.session.completed':
        return {'ignored': event.get('type')}
    session = event['data']['object']
    metadata = session.get('metadata') or {}
    user = metadata.get('user') or session.get('client_reference_id')
    credits = int(metadata.get('credits') or (session.get('amount_total') or 0) * CREDITS_PER_CENT)
    result = _grant(ledger, user, credits, key=event['id'])
    result['customer'] = session.get('customer')
    return result
