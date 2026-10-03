import json
import os
import sys
import tempfile
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard
from ledger import Ledger
from market import Market
from accounts import Accounts


class Client:
    """Minimal cookie-aware HTTP client so session continuity can be tested."""
    def __init__(self, base):
        self.base = base
        self.cookie = None

    def request(self, method, path, body=None):
        headers = {'Content-Type': 'application/json'}
        if self.cookie:
            headers['Cookie'] = self.cookie
        req = urllib.request.Request(self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            response = urllib.request.urlopen(req)
            status, raw, setc = response.status, response.read(), response.headers.get('Set-Cookie')
        except urllib.error.HTTPError as error:
            status, raw, setc = error.code, error.read(), error.headers.get('Set-Cookie')
        if setc:
            self.cookie = setc.split(';', 1)[0]
        return status, json.loads(raw or b'{}')


class DashboardBettingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.patches = [
            patch.object(dashboard, 'RUNS', root),
            patch.object(dashboard, 'LEDGER', Ledger(root / 'ledger.jsonl')),
            patch.object(dashboard, 'MARKET', Market(Ledger(root / 'ledger.jsonl'), path=root / 'market.json')),
            patch.object(dashboard, 'ACCOUNTS', Accounts(root / 'secret', root / 'links.jsonl')),
            patch.object(dashboard, 'LAST', root / 'latest.json'),
            patch.object(dashboard.payments, 'ENV_PATH', root / 'empty.env'),
            patch.dict(os.environ, {'ARENA_DEV_CREDITS': '1'}),
        ]
        for p in self.patches:
            p.start()
        # Share one ledger between dashboard and market.
        dashboard.MARKET._ledger = dashboard.LEDGER
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), dashboard.Handler)
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        for p in reversed(self.patches):
            p.stop()
        self.dir.cleanup()

    def running_match(self):
        dashboard.STATE.update(phase='running', match_id='t1', started_at=time.time(), limit=300,
            outcome=None, winner=None, prediction={'status': 'live', 'probabilities': {'a': 0.5, 'b': 0.5}},
            players={'a': {'name': 'A', 'id': 'a', 'alive': True, 'state': 'standing'},
                     'b': {'name': 'B', 'id': 'b', 'alive': True, 'state': 'standing'}})
        dashboard.MARKET.open('t1', ['a', 'b'])

    def test_credits_issues_a_session_cookie(self):
        client = Client(self.base)
        status, data = client.request('GET', '/api/credits')
        self.assertEqual(status, 200)
        self.assertEqual(data['balance'], 0)
        self.assertTrue(client.cookie.startswith('arena_session=u_'))

    def test_two_clients_get_distinct_identities(self):
        a, b = Client(self.base), Client(self.base)
        a.request('GET', '/api/credits'); b.request('GET', '/api/credits')
        self.assertNotEqual(a.cookie, b.cookie)

    def test_bet_uses_session_identity_not_the_request_body(self):
        self.running_match()
        client = Client(self.base)
        # Establish session + fund it via the dev top-up (authenticated by cookie).
        client.request('GET', '/api/credits')
        client.request('POST', '/api/stripe/webhook', {'credits': 1000})
        # Attempt to charge a *different* user id in the body — must be ignored.
        status, data = client.request('POST', '/api/bet', {'user': 'victim', 'outcome': 'a', 'stake': 100})
        self.assertEqual(status, 200)
        self.assertEqual(data['balance'], 900)                 # the session user paid
        self.assertEqual(dashboard.LEDGER.balance('victim'), 0)  # the named victim did not
        self.assertEqual(dashboard.MARKET.pools['a'], 100)

    def test_dev_topup_credits_the_session_user(self):
        client = Client(self.base)
        client.request('GET', '/api/credits')
        status, data = client.request('POST', '/api/stripe/webhook', {'credits': 250})
        self.assertEqual(status, 200)
        _, credits = client.request('GET', '/api/credits')
        self.assertEqual(credits['balance'], 250)

    def test_bet_rejected_when_no_market_is_open(self):
        client = Client(self.base)
        client.request('GET', '/api/credits')
        client.request('POST', '/api/stripe/webhook', {'credits': 500})
        status, _ = client.request('POST', '/api/bet', {'outcome': 'a', 'stake': 50})
        self.assertEqual(status, 409)

    def test_market_recovers_and_refunds_after_a_restart(self):
        self.running_match()
        client = Client(self.base)
        client.request('GET', '/api/credits')
        client.request('POST', '/api/stripe/webhook', {'credits': 500})
        client.request('POST', '/api/bet', {'outcome': 'a', 'stake': 200})
        uid = dashboard.ACCOUNTS.verify(client.cookie.split('=', 1)[1])
        self.assertEqual(dashboard.LEDGER.balance(uid), 300)
        # Simulate a restart: a fresh Market from the same file, same ledger.
        root = Path(self.dir.name)
        reopened = Market(dashboard.LEDGER, path=root / 'market.json')
        self.assertEqual(reopened.status, 'open')  # persisted across the "restart"
        result = reopened.recover()
        self.assertEqual(result['status'], 'void')
        self.assertEqual(dashboard.LEDGER.balance(uid), 500)  # stake refunded


if __name__ == '__main__':
    unittest.main()
