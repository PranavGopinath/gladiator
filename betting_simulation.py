"""Isolated, explicitly synthetic betting pools. Never uses the Stripe ledger."""
import random
import threading
import time
from ledger import Ledger
from market import Market


class DemoBook:
    def __init__(self, seed=None):
        self.ledger = Ledger()
        self.markets = {}
        self.match_id = None
        self.lock = threading.RLock()
        self.rng = random.Random(seed)
        self.activity = []
        self.next_bet = 0
        self.bots = [('Early Bird', 'early'), ('Momentum', 'momentum'), ('Contrarian', 'contrarian'),
                     ('Clock Watcher', 'cautious'), ('Lime Fan', 'fan'), ('Orange Fan', 'fan'),
                     ('Value Hunter', 'contrarian'), ('Patient Fan', 'cautious'),
                     ('Trend Follower', 'momentum'), ('First Mover', 'early')]

    def update(self, state, quotes, now=None):
        now = time.time() if now is None else now
        with self.lock:
            if state.get('experiment'):
                for market in self.markets.values():
                    if market.status in ('open', 'closed'):
                        market.void('Experiment matches do not accept bets')
                self.markets = {}; self.activity = []; self.match_id = state.get('match_id')
                return
            if state.get('match_id') != self.match_id:
                for m in self.markets.values():
                    if m.status in ('open', 'closed'):
                        m.void('Previous demo market interrupted')
                self.match_id = state.get('match_id')
                self.markets = {}; self.activity = []; self.next_bet = now
                if self.match_id and state['phase'] == 'running':
                    for name, quote in quotes.items():
                        m = Market(self.ledger, name=name)
                        m.open(self.match_id, list(quote['outcomes']), draw=False)
                        self.markets[name] = m
                    for i in range(len(self.bots)):
                        self.ledger.post('bot-' + str(i), 2500, 'adjust', key=f'{self.match_id}:fund:{i}')
            # A visitor may first arrive during preparation, before books open.
            if self.match_id and not self.markets and state['phase'] == 'running':
                self.match_id = None
                return self.update(state, quotes, now)
            for name, m in self.markets.items():
                q = quotes[name]
                if q['status'] in ('settled', 'void') and m.status in ('open', 'closed'):
                    winner = (q.get('result') or {}).get('winning_outcome')
                    if winner and winner != 'draw':
                        m.settle(winner, 'winner')
                    else:
                        m.void('Referee voided the market')
                elif state['phase'] in ('finished', 'error', 'interrupted') and m.status == 'open':
                    m.void('Match ended without a scored market')
            if state['phase'] != 'running' or now < self.next_bet:
                return
            choices = [(n, q) for n, q in quotes.items() if q['status'] == 'open' and any(o['open'] for o in q['outcomes'].values())]
            self.next_bet = now + self.rng.uniform(2, 6)
            if not choices:
                return
            name, q = self.rng.choice(choices)
            i = self.rng.randrange(len(self.bots)); label, style = self.bots[i]
            ids = [p for p, o in q['outcomes'].items() if o['open']]
            prediction = q.get('_prediction') if '_prediction' in q else {'status': 'live', 'probabilities': {p: o['price'] for p, o in q['outcomes'].items()}}
            demo_quote = self.markets[name].quote(prediction, set(ids))
            weights = [demo_quote['outcomes'][p]['price'] for p in ids]
            if style == 'contrarian': weights = [1 / max(.05, w) for w in weights]
            if style == 'fan': weights = [4 if j == i % len(ids) else 1 for j in range(len(ids))]
            if style == 'early': weights = [1 for _ in ids]
            if style == 'cautious': weights = [w * (3 if p in ('draw', 'nobody') else 1) for p, w in zip(ids, weights)]
            outcome = self.rng.choices(ids, weights=weights)[0]
            user = 'bot-' + str(i); stake = min(self.ledger.balance(user), self.rng.choice([10, 20, 25, 50, 75]))
            if stake <= 0: return
            prediction = q.get('_prediction') if '_prediction' in q else {'status': 'live', 'probabilities': {p: o['price'] for p, o in q['outcomes'].items()}}
            self.markets[name].place_bet(user, outcome, stake, prediction, set(ids))
            self.activity.append({'bot': label, 'market': name, 'outcome': outcome, 'stake': stake, 'ts': now})
            self.activity = self.activity[-12:]

    def fund(self, user):
        with self.lock:
            self.ledger.post(user, 1000, 'adjust')
