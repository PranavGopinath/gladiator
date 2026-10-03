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
from betting_simulation import DemoBook


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
        ledger = Ledger(root / 'ledger.jsonl')
        winner = Market(ledger, path=root / 'market.json', name='winner')
        first_blood = Market(ledger, path=root / 'market-first-blood.json', name='first_blood')
        first_fallen = Market(ledger, path=root / 'market-first-fallen.json', name='first_fallen')
        self.patches = [
            patch.object(dashboard, 'RUNS', root),
            patch.object(dashboard, 'LEDGER', ledger),
            patch.object(dashboard, 'DEMO_BOOK', DemoBook(seed=7)),
            patch.object(dashboard, 'MARKET', winner),
            patch.object(dashboard, 'FIRST_BLOOD', first_blood),
            patch.object(dashboard, 'FIRST_FALLEN', first_fallen),
            patch.object(dashboard, 'MARKETS', {'winner': winner, 'first_blood': first_blood, 'first_fallen': first_fallen}),
            patch.object(dashboard, 'ACCOUNTS', Accounts(root / 'secret', root / 'links.jsonl')),
            patch.object(dashboard, 'LAST', root / 'latest.json'),
            patch.object(dashboard.payments, 'ENV_PATH', root / 'empty.env'),
            patch.dict(os.environ, {'ARENA_DEV_CREDITS': '1'}),
        ]
        for p in self.patches:
            p.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), dashboard.Handler)
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        for p in reversed(self.patches):
            p.stop()
        self.dir.cleanup()

    def running_match(self):
        dashboard.STATE.update(phase='running', match_id='t1', started_at=time.time(), limit=300, events=[], event_seq=0,
            outcome=None, winner=None, experiment=None, prediction={'status': 'live', 'probabilities': {'a': 0.5, 'b': 0.5}},
            players={'a': {'name': 'A', 'id': 'a', 'alive': True, 'state': 'standing'},
                     'b': {'name': 'B', 'id': 'b', 'alive': True, 'state': 'standing'}})
        dashboard.MARKET.open('t1', ['a', 'b'], draw=False)
        dashboard.FIRST_BLOOD.open('t1', ['a', 'b', 'nobody'], draw=False)
        dashboard.FIRST_FALLEN.open('t1', ['a', 'b', 'nobody'], draw=False)

    def kill(self, victim, attacker, at=None, confidence='confirmed'):
        info = dashboard.STATE['players'][victim]
        info.update(alive=False, state='eliminated', activity='eliminated', death_at=at or time.time())
        info['elimination'] = {'cause': 'attack' if attacker else 'session_exited', 'confidence': confidence if attacker else 'observed',
                               'attacker': attacker, 'evidence_seqs': [], 'summary': 'test'}

    def funded(self, credits=1000):
        client = Client(self.base)
        client.request('GET', '/api/credits')
        client.request('POST', '/api/stripe/webhook', {'credits': credits})
        return client

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

    def test_draw_refunds_all_winner_tickets(self):
        self.running_match()
        a, b = self.funded(), self.funded()
        a.request('POST', '/api/bet', {'outcome': 'a', 'stake': 100})
        b.request('POST', '/api/bet', {'outcome': 'b', 'stake': 200})
        status, _ = a.request('POST', '/api/bet', {'outcome': 'draw', 'stake': 10})
        self.assertEqual(status, 409)
        dashboard.STATE.update(phase='finishing', outcome='draw', winner=None)
        dashboard.settle_market()
        self.assertEqual(dashboard.MARKET.status, 'void')
        self.assertEqual(a.request('GET', '/api/credits')[1]['balance'], 1000)
        self.assertEqual(b.request('GET', '/api/credits')[1]['balance'], 1000)

    def test_final_ten_seconds_rejects_bets(self):
        self.running_match()
        client = self.funded()
        dashboard.STATE['started_at'] = time.time() - 295
        status, _ = client.request('POST', '/api/bet', {'outcome': 'a', 'stake': 100})
        self.assertEqual(status, 409)

    def test_simulation_flag_uses_existing_endpoints_and_isolates_balances(self):
        self.running_match()
        client = self.funded()
        with patch.object(dashboard, 'SIMULATE_BETTORS', True):
            _, board = client.request('GET', '/api/market')
            self.assertTrue(board['demo'])
            self.assertEqual(board['bot_count'], 10)
            self.assertEqual(set(board['winner']['outcomes']), {'a', 'b'})
            self.assertFalse(client.request('GET', '/api/credits')[1]['stripe'])
            client.request('POST', '/api/stripe/webhook', {})
            status, _ = client.request('POST', '/api/bet', {'outcome': 'a', 'stake': 100})
            self.assertEqual(status, 200)
            self.assertEqual(client.request('GET', '/api/position')[1]['balance'], 900)
            self.assertEqual(client.request('POST', '/api/checkout', {'pack': 'small'})[0], 409)
        self.assertEqual(client.request('GET', '/api/credits')[1]['balance'], 1000)
        self.assertEqual(dashboard.MARKET.bets, [])
        dashboard.STATE.update(phase='finishing', outcome='draw', winner=None)
        dashboard.settle_market()
        with patch.object(dashboard, 'SIMULATE_BETTORS', True):
            client.request('GET', '/api/market')
            self.assertEqual(client.request('GET', '/api/position')[1]['balance'], 1000)

    def test_experiments_reject_bets_in_every_market_and_simulation(self):
        self.running_match()
        client = self.funded()
        # Keep a previous open book around to test server gating, not just UI hiding.
        dashboard.STATE['experiment'] = {'id': 'fixture', 'arm': 'training'}
        for name in dashboard.MARKETS:
            status, _ = client.request('POST', '/api/bet', {'market': name, 'outcome': 'a', 'stake': 100})
            self.assertEqual(status, 409)
        with patch.object(dashboard, 'SIMULATE_BETTORS', True):
            client.request('POST', '/api/stripe/webhook', {})
            client.request('GET', '/api/market')
            self.assertEqual(dashboard.DEMO_BOOK.markets, {})
            status, _ = client.request('POST', '/api/bet', {'outcome': 'a', 'stake': 100})
            self.assertEqual(status, 409)
        self.assertEqual(dashboard.LEDGER.balance(client.request('GET', '/api/credits')[1]['user']), 1000)
        self.assertTrue(all(not m.bets for m in dashboard.MARKETS.values()))
        dashboard.STATE['experiment'] = None

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

    def test_market_board_lists_both_markets(self):
        self.running_match()
        _, board = Client(self.base).request('GET', '/api/market')
        self.assertEqual(board['match_id'], 't1')
        self.assertEqual(set(board['winner']['outcomes']), {'a', 'b'})
        self.assertEqual(set(board['first_blood']['outcomes']), {'a', 'b', 'nobody'})
        self.assertEqual(set(board['first_fallen']['outcomes']), {'a', 'b', 'nobody'})
        self.assertTrue(board['first_blood']['outcomes']['a']['open'])
        self.assertIn('weight', board['winner']['outcomes']['a'])

    def test_kill_markets_are_priced_from_jev_factors(self):
        self.running_match()
        dashboard.STATE['prediction'] = {'status': 'live', 'probabilities': {'a': 0.5, 'b': 0.5},
                                         'factors': {'a': {'danger': 0.75, 'progress': 0.2}, 'b': {'danger': 0.25, 'progress': 0.6}}}
        _, board = Client(self.base).request('GET', '/api/market')
        self.assertAlmostEqual(board['first_fallen']['outcomes']['a']['price'], 0.75)   # danger
        self.assertAlmostEqual(board['first_blood']['outcomes']['b']['price'], 0.75)    # progress

    def test_first_fallen_settles_mid_match_on_the_first_elimination(self):
        self.running_match()
        backer, other = self.funded(), self.funded()
        status, _ = backer.request('POST', '/api/bet', {'market': 'first_fallen', 'outcome': 'a', 'stake': 100})
        self.assertEqual(status, 200)
        other.request('POST', '/api/bet', {'market': 'first_fallen', 'outcome': 'b', 'stake': 300})
        _, position = backer.request('GET', '/api/position')
        live = position['positions']['first_fallen'][0]
        self.assertEqual(live['status'], 'live')
        self.assertEqual(live['projected'], 400)  # sole winning weight takes the whole pool
        self.kill('a', None)  # session exited, nobody credited
        dashboard.settle_kill_markets()
        self.assertEqual(dashboard.FIRST_FALLEN.status, 'settled')
        self.assertEqual(dashboard.FIRST_BLOOD.status, 'open')  # a death without an attacker is not a kill
        _, credits = backer.request('GET', '/api/credits')
        self.assertEqual(credits['balance'], 1300)
        _, position = backer.request('GET', '/api/position')
        self.assertEqual(position['positions']['first_fallen'][0]['status'], 'won')
        self.assertEqual(position['positions']['first_fallen'][0]['returned'], 400)
        self.assertEqual([e['reason'] for e in position['history']], ['purchase', 'bet', 'payout'])
        self.assertEqual(dashboard.MARKET.status, 'open')
        status, _ = other.request('POST', '/api/bet', {'market': 'first_fallen', 'outcome': 'b', 'stake': 10})
        self.assertEqual(status, 409)

    def test_first_blood_pays_the_attacker_of_the_first_attributed_kill(self):
        self.running_match()
        killer_backer, other = self.funded(), self.funded()
        killer_backer.request('POST', '/api/bet', {'market': 'first_blood', 'outcome': 'b', 'stake': 100})
        other.request('POST', '/api/bet', {'market': 'first_blood', 'outcome': 'a', 'stake': 100})
        self.kill('a', 'b')  # b eliminates a
        dashboard.settle_kill_markets()
        self.assertEqual(dashboard.FIRST_BLOOD.status, 'settled')
        self.assertEqual(dashboard.FIRST_BLOOD._result['winning_outcome'], 'b')
        self.assertEqual(dashboard.FIRST_FALLEN._result['winning_outcome'], 'a')
        _, credits = killer_backer.request('GET', '/api/credits')
        self.assertEqual(credits['balance'], 1100)

    def test_first_blood_waits_for_late_attribution_then_looks_past_an_unattributed_death(self):
        self.running_match()
        self.funded().request('POST', '/api/bet', {'market': 'first_blood', 'outcome': 'a', 'stake': 50})
        self.funded().request('POST', '/api/bet', {'market': 'first_blood', 'outcome': 'nobody', 'stake': 50})
        self.kill('b', None)                     # fresh, unattributed: grace period keeps the market open
        dashboard.settle_kill_markets()
        self.assertEqual(dashboard.FIRST_BLOOD.status, 'open')
        dashboard.STATE['players']['b']['elimination']['attacker'] = 'a'   # attribution arrives late
        dashboard.settle_kill_markets()
        self.assertEqual(dashboard.FIRST_BLOOD._result['winning_outcome'], 'a')

    def test_unattributed_death_past_grace_does_not_block_a_later_kill(self):
        self.running_match()
        dashboard.STATE['players']['c'] = {'name': 'C', 'id': 'c', 'alive': True, 'state': 'standing'}
        for m in (dashboard.MARKET, dashboard.FIRST_BLOOD, dashboard.FIRST_FALLEN):
            m.open('t1', ['a', 'b', 'c'] + (['nobody'] if m is not dashboard.MARKET else []), draw=m is dashboard.MARKET)
        self.funded().request('POST', '/api/bet', {'market': 'first_blood', 'outcome': 'c', 'stake': 50})
        self.funded().request('POST', '/api/bet', {'market': 'first_blood', 'outcome': 'a', 'stake': 50})
        self.kill('a', None, at=time.time() - 10)  # old, never attributed
        self.kill('b', 'c')
        dashboard.settle_kill_markets()
        self.assertEqual(dashboard.FIRST_BLOOD._result['winning_outcome'], 'c')
        self.assertEqual(dashboard.FIRST_FALLEN._result['winning_outcome'], 'a')

    def test_simultaneous_deaths_void_first_fallen(self):
        self.running_match()
        self.funded().request('POST', '/api/bet', {'market': 'first_fallen', 'outcome': 'a', 'stake': 50})
        now = time.time(); self.kill('a', None, at=now); self.kill('b', None, at=now + 0.01)
        dashboard.settle_kill_markets()
        self.assertEqual(dashboard.FIRST_FALLEN.status, 'void')

    def test_first_blood_legacy_assertions_hold(self):
        self.running_match()
        backer, other = self.funded(), self.funded()
        backer.request('POST', '/api/bet', {'market': 'first_blood', 'outcome': 'b', 'stake': 100})
        other.request('POST', '/api/bet', {'market': 'first_blood', 'outcome': 'a', 'stake': 300})
        self.kill('a', 'b')
        dashboard.settle_kill_markets()
        self.assertEqual(dashboard.FIRST_BLOOD.status, 'settled')
        _, credits = backer.request('GET', '/api/credits')
        self.assertEqual(credits['balance'], 1300)
        self.assertEqual(dashboard.MARKET.status, 'open')
        status, _ = other.request('POST', '/api/bet', {'market': 'first_blood', 'outcome': 'b', 'stake': 10})
        self.assertEqual(status, 409)

    def test_kill_markets_pay_nobody_when_the_match_ends_without_an_elimination(self):
        self.running_match()
        pessimist, optimist = self.funded(), self.funded()
        pessimist.request('POST', '/api/bet', {'market': 'first_blood', 'outcome': 'a', 'stake': 100})
        optimist.request('POST', '/api/bet', {'market': 'first_blood', 'outcome': 'nobody', 'stake': 100})
        optimist.request('POST', '/api/bet', {'market': 'first_fallen', 'outcome': 'nobody', 'stake': 100})
        dashboard.STATE.update(phase='finishing', outcome='draw', winner=None)
        dashboard.settle_market()
        self.assertEqual(dashboard.FIRST_BLOOD.status, 'settled')
        self.assertEqual(dashboard.FIRST_FALLEN.status, 'void')  # one-sided pool: refunded
        self.assertEqual(dashboard.MARKET.status, 'void')  # nobody backed the draw
        _, credits = optimist.request('GET', '/api/credits')
        self.assertEqual(credits['balance'], 1100)
        kinds = [e['data'].get('market') for e in dashboard.STATE['events'] if e['kind'] == 'market' and e['data'].get('status') in ('settled', 'void')]
        self.assertEqual(sorted(kinds), ['first_blood', 'first_fallen', 'winner'])

    def test_canceled_match_refunds_every_market(self):
        self.running_match()
        c = self.funded()
        c.request('POST', '/api/bet', {'market': 'first_blood', 'outcome': 'a', 'stake': 100})
        c.request('POST', '/api/bet', {'market': 'winner', 'outcome': 'b', 'stake': 100})
        dashboard.STATE.update(phase='finishing', outcome='canceled', winner=None)
        dashboard.settle_market()
        self.assertEqual({m.status for m in dashboard.MARKETS.values()}, {'void'})
        _, credits = c.request('GET', '/api/credits')
        self.assertEqual(credits['balance'], 1000)

    def test_unknown_market_is_rejected(self):
        self.running_match()
        status, _ = self.funded().request('POST', '/api/bet', {'market': 'over_under', 'outcome': 'a', 'stake': 10})
        self.assertEqual(status, 400)


if __name__ == '__main__':
    unittest.main()
