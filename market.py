"""Snapshot pari-mutuel betting market for arena matches.

The market carries zero liability: every payout is a share of a fixed pool, so
the system can never owe more than was staked. Each bet's payout weight is frozen
to the live price at placement time (Jev's win probability when available, the
pool-implied price otherwise), which neutralises the late-money advantage of
in-play betting — conviction when the outcome was uncertain pays more than piling
onto a near-certain winner.

The market only ever prices and pays. It never decides who won: settlement is
driven by the host-side referee outcome passed into `settle`. A Jev outage
degrades to pool-implied pricing; it never blocks a bet or a payout.
"""
import json
from pathlib import Path
import threading
import time

PRICE_FLOOR = 0.01  # Clamp prices so a near-zero probability cannot mint huge weights.
DRAW = 'draw'
_PERSIST = ('match_id', 'status', 'outcomes', 'pools', 'bets', '_seq', '_result', '_rake_bps')


class MarketError(Exception):
    pass


def price_for(outcome, prediction, pools):
    """Live price for an outcome in (0, 1]. Jev's probability when it is live and
    quotes this outcome; otherwise the pool-implied share, or a uniform prior on
    an empty pool. Jev never quotes a draw, so draw is always pool-implied."""
    if prediction and prediction.get('status') == 'live':
        probabilities = prediction.get('probabilities') or {}
        value = probabilities.get(outcome)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 1:
            return max(PRICE_FLOOR, min(1.0, float(value)))
    total = sum(pools.values())
    if total > 0:
        return max(PRICE_FLOOR, min(1.0, pools[outcome] / total))
    return max(PRICE_FLOOR, 1.0 / len(pools)) if pools else 1.0


def _apportion(total, weights):
    """Split an integer pool across weighted winners with no credit lost: floor
    every share, then hand the remainder to the largest fractional parts."""
    if total <= 0 or not weights:
        return {key: 0 for key in weights}
    mass = sum(weights.values())
    exact = {key: total * weight / mass for key, weight in weights.items()}
    shares = {key: int(value) for key, value in exact.items()}
    remainder = total - sum(shares.values())
    for key in sorted(exact, key=lambda k: (exact[k] - shares[k], k), reverse=True)[:remainder]:
        shares[key] += 1
    return shares


class Market:
    def __init__(self, ledger, rake_bps=0, path=None):
        self._ledger = ledger
        self._lock = threading.RLock()
        self._rake_bps = rake_bps
        self._path = Path(path) if path else None
        self.match_id = None
        self.status = 'idle'          # idle | open | closed | settled | void
        self.outcomes = []            # contestant ids + 'draw'
        self.pools = {}
        self.bets = []
        self._seq = 0
        if self._path and self._path.exists():
            self._load()

    def _load(self):
        try:
            state = json.loads(self._path.read_text())
        except (ValueError, OSError):
            return
        for field in _PERSIST:
            if field in state:
                setattr(self, field, state[field])

    def _save(self):
        if not self._path:
            return
        snapshot = {field: getattr(self, field) for field in _PERSIST}
        temp = self._path.with_suffix('.tmp')
        temp.write_text(json.dumps(snapshot))
        temp.replace(self._path)

    def recover(self, reason='Market interrupted before settlement'):
        """Called on startup: a market left open/closed by a crash can never be
        refereed, so refund every stake. Idempotent via the ledger refund keys."""
        with self._lock:
            if self.status in ('open', 'closed'):
                return self._void(reason)
            return None

    # -- lifecycle -------------------------------------------------------------
    def open(self, match_id, contestants, draw=True):
        with self._lock:
            self.match_id = match_id
            self.outcomes = list(contestants) + ([DRAW] if draw else [])
            self.pools = {outcome: 0 for outcome in self.outcomes}
            self.bets = []
            self._seq = 0
            self.status = 'open'
            self._save()

    def place_bet(self, user, outcome, stake, prediction=None, open_outcomes=None, event_seq=None, key=None):
        """Accept a credit stake on an outcome. `open_outcomes` is the authoritative
        set of still-bettable outcomes derived from referee liveness; it — not the
        slower Jev feed — is what closes a market the instant a contestant dies, so
        the feed lag cannot be exploited."""
        with self._lock:
            if self.status != 'open':
                raise MarketError('Betting is closed')
            if outcome not in self.pools:
                raise MarketError('Unknown outcome')
            if open_outcomes is not None and outcome not in open_outcomes:
                raise MarketError('That outcome is no longer open')
            stake = int(stake)
            if stake <= 0:
                raise MarketError('Stake must be a positive number of credits')
            price = price_for(outcome, prediction, self.pools)
            # Debit first: if the better lacks credits the pool is left untouched.
            self._ledger.post(user, -stake, 'bet', key=key, match_id=self.match_id,
                              outcome=outcome, price=price)
            self._seq += 1
            bet = {'seq': self._seq, 'ts': time.time(), 'user': str(user), 'outcome': outcome,
                   'stake': stake, 'price': price, 'weight': stake / price, 'event_seq': event_seq}
            self.bets.append(bet)
            self.pools[outcome] += stake
            self._save()
            return bet

    def close(self):
        """Stop accepting bets without settling (e.g. the deadline passed)."""
        with self._lock:
            if self.status == 'open':
                self.status = 'closed'
                self._save()

    def settle(self, outcome, kind):
        """Pay winners from the pool using the referee result. `kind` is the match
        outcome type: 'winner', 'draw', or anything else (canceled/invalid) which
        voids the market and refunds every stake. Idempotent per match."""
        with self._lock:
            if self.status in ('settled', 'void'):
                return self._result
            if kind == 'winner':
                winning = outcome
            elif kind == 'draw':
                winning = DRAW
            else:
                return self._void('Match did not produce a scored result')
            funded = {o for o, amount in self.pools.items() if amount > 0}
            total = sum(self.pools.values())
            # No winning stake, or no opposing side: there is nothing to win, so
            # return every stake rather than redistribute among one camp.
            if self.pools.get(winning, 0) <= 0 or len(funded) < 2:
                return self._void('No opposing pool to settle against')
            distributable = total - total * self._rake_bps // 10000
            weights = {bet['seq']: bet['weight'] for bet in self.bets if bet['outcome'] == winning}
            shares = _apportion(distributable, weights)
            payouts = {}
            for bet in self.bets:
                amount = shares.get(bet['seq'], 0)
                if amount:
                    self._ledger.post(bet['user'], amount, 'payout',
                                      key=f'{self.match_id}:payout:{bet["seq"]}',
                                      match_id=self.match_id, outcome=winning, bet_seq=bet['seq'])
                    payouts[bet['user']] = payouts.get(bet['user'], 0) + amount
            self.status = 'settled'
            self._result = {'status': 'settled', 'winning_outcome': winning, 'pool': total,
                            'distributed': sum(shares.values()), 'rake': total - distributable,
                            'payouts': payouts}
            self._save()
            return self._result

    def void(self, reason='Match voided'):
        with self._lock:
            return self._void(reason)

    def _void(self, reason):
        for bet in self.bets:
            self._ledger.post(bet['user'], bet['stake'], 'refund',
                              key=f'{self.match_id}:refund:{bet["seq"]}',
                              match_id=self.match_id, note=reason)
        self.status = 'void'
        self._result = {'status': 'void', 'reason': reason,
                        'refunded': sum(bet['stake'] for bet in self.bets)}
        self._save()
        return self._result

    # -- display ---------------------------------------------------------------
    def quote(self, prediction=None, open_outcomes=None):
        """Live board: pool, frozen-at-now price and decimal odds per outcome."""
        with self._lock:
            total = sum(self.pools.values())
            board = {}
            for outcome in self.outcomes:
                price = price_for(outcome, prediction, self.pools)
                board[outcome] = {
                    'pool': self.pools[outcome],
                    'implied': (self.pools[outcome] / total) if total else None,
                    'price': price,
                    'odds': round(1 / price, 3),
                    'open': open_outcomes is None or outcome in open_outcomes,
                }
            return {'match_id': self.match_id, 'status': self.status, 'total_pool': total,
                    'rake_bps': self._rake_bps, 'outcomes': board, 'bet_count': len(self.bets)}

    _result = None
