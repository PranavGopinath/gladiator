import hashlib
import hmac
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import payments
from ledger import Ledger


def signed(body, secret, timestamp=None):
    timestamp = timestamp or int(time.time())
    signature = hmac.new(secret.encode(), f'{timestamp}.'.encode() + body.encode(), hashlib.sha256).hexdigest()
    return f't={timestamp},v1={signature}'


class FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


class PaymentsTests(unittest.TestCase):
    def setUp(self):
        # Isolate from the real .env and environment for every test.
        self.env = tempfile.NamedTemporaryFile('w', suffix='.env', delete=False)
        self.env.close()
        self.path_patch = patch.object(payments, 'ENV_PATH', Path(self.env.name))
        self.path_patch.start()
        self.environ = patch.dict(os.environ, {}, clear=True)
        self.environ.start()

    def tearDown(self):
        self.path_patch.stop(); self.environ.stop(); os.unlink(self.env.name)

    def write_env(self, text):
        Path(self.env.name).write_text(text)

    def test_configured_reads_key_from_env_file(self):
        self.assertFalse(payments.configured())
        self.write_env('STRIPE_SECRET_KEY=sk_test_abc # comment\n')
        self.assertTrue(payments.configured())

    def test_create_checkout_posts_expected_fields_and_parses_url(self):
        self.write_env('STRIPE_SECRET_KEY="sk_test_abc"\n')
        seen = {}
        def fake_open(request, timeout=0):
            seen['url'] = request.full_url
            seen['auth'] = request.headers.get('Authorization')
            seen['body'] = request.data.decode()
            return FakeResponse(json.dumps({'id': 'cs_test_1', 'url': 'https://checkout.stripe.com/c/pay/cs_test_1'}).encode())
        with patch.object(payments, '_urlopen', fake_open):
            session = payments.create_checkout('fan-1', 'small', 'http://x/ok', 'http://x/no')
        self.assertEqual(session['url'], 'https://checkout.stripe.com/c/pay/cs_test_1')
        self.assertEqual(seen['url'], 'https://api.stripe.com/v1/checkout/sessions')
        self.assertEqual(seen['auth'], 'Bearer sk_test_abc')
        self.assertIn('metadata%5Buser%5D=fan-1', seen['body'])
        self.assertIn('unit_amount%5D=500', seen['body'])

    def test_create_checkout_requires_a_key(self):
        with self.assertRaises(payments.PaymentError):
            payments.create_checkout('fan-1', 'small', 'http://x/ok', 'http://x/no')

    def test_webhook_grants_credits_on_valid_signature(self):
        secret = 'whsec_test'
        self.write_env(f'STRIPE_SECRET_KEY=sk_test_abc\nSTRIPE_WEBHOOK_SECRET={secret}\n')
        ledger = Ledger()
        body = json.dumps({'id': 'evt_1', 'type': 'checkout.session.completed',
                           'data': {'object': {'metadata': {'user': 'fan-1', 'credits': '500'}}}})
        result = payments.handle_webhook(body, signed(body, secret), ledger)
        self.assertEqual(result, {'user': 'fan-1', 'credits': 500, 'customer': None})
        self.assertEqual(ledger.balance('fan-1'), 500)

    def test_webhook_is_idempotent_on_event_id(self):
        secret = 'whsec_test'
        self.write_env(f'STRIPE_SECRET_KEY=sk_test_abc\nSTRIPE_WEBHOOK_SECRET={secret}\n')
        ledger = Ledger()
        body = json.dumps({'id': 'evt_1', 'type': 'checkout.session.completed',
                           'data': {'object': {'metadata': {'user': 'fan-1', 'credits': '500'}}}})
        payments.handle_webhook(body, signed(body, secret), ledger)
        payments.handle_webhook(body, signed(body, secret), ledger)
        self.assertEqual(ledger.balance('fan-1'), 500)

    def test_webhook_rejects_a_tampered_body(self):
        secret = 'whsec_test'
        self.write_env(f'STRIPE_SECRET_KEY=sk_test_abc\nSTRIPE_WEBHOOK_SECRET={secret}\n')
        ledger = Ledger()
        body = json.dumps({'id': 'evt_1', 'type': 'checkout.session.completed',
                           'data': {'object': {'metadata': {'user': 'fan-1', 'credits': '500'}}}})
        header = signed(body, secret)
        tampered = body.replace('500', '500000')
        with self.assertRaises(payments.PaymentError):
            payments.handle_webhook(tampered, header, ledger)
        self.assertEqual(ledger.balance('fan-1'), 0)

    def test_webhook_rejects_stale_timestamp(self):
        secret = 'whsec_test'
        self.write_env(f'STRIPE_SECRET_KEY=sk_test_abc\nSTRIPE_WEBHOOK_SECRET={secret}\n')
        ledger = Ledger()
        body = json.dumps({'id': 'evt_1', 'type': 'checkout.session.completed',
                           'data': {'object': {'metadata': {'user': 'fan-1', 'credits': '500'}}}})
        old = signed(body, secret, timestamp=int(time.time()) - 10000)
        with self.assertRaises(payments.PaymentError):
            payments.handle_webhook(body, old, ledger)

    def test_non_checkout_events_are_ignored(self):
        secret = 'whsec_test'
        self.write_env(f'STRIPE_SECRET_KEY=sk_test_abc\nSTRIPE_WEBHOOK_SECRET={secret}\n')
        ledger = Ledger()
        body = json.dumps({'id': 'evt_2', 'type': 'payment_intent.created', 'data': {'object': {}}})
        result = payments.handle_webhook(body, signed(body, secret), ledger)
        self.assertEqual(result, {'ignored': 'payment_intent.created'})
        self.assertEqual(ledger.balances(), {})

    def test_dev_mode_grants_without_a_signature_when_no_key(self):
        with patch.dict(os.environ, {'ARENA_DEV_CREDITS': '1'}):
            ledger = Ledger()
            result = payments.handle_webhook(json.dumps({'user': 'fan-1', 'credits': 250, 'id': 'd1'}), '', ledger)
            self.assertEqual(result, {'user': 'fan-1', 'credits': 250})
            self.assertEqual(ledger.balance('fan-1'), 250)

    def test_real_key_disables_dev_mode(self):
        self.write_env('STRIPE_SECRET_KEY=sk_test_abc\n')
        with patch.dict(os.environ, {'ARENA_DEV_CREDITS': '1'}):
            self.assertFalse(payments.dev_mode())


if __name__ == '__main__':
    unittest.main()
