"""Append-only credit ledger for arena betting.

Balances are never stored; they are the sum of immutable entries, so every
movement is auditable and a replayed Stripe webhook or match settlement is a
no-op. Credits are an entertainment currency bought through Stripe; this module
never touches cash and never pays out money. All amounts are integer credits to
avoid floating-point drift in balances.
"""
import json
from pathlib import Path
import threading
import time

REASONS = ('purchase', 'bet', 'payout', 'refund', 'adjust')


class LedgerError(Exception):
    pass


class Ledger:
    """Thread-safe, append-only, idempotent credit ledger backed by a JSONL file."""

    def __init__(self, path=None):
        self._lock = threading.RLock()
        self._path = Path(path) if path else None
        self._entries = []
        self._balances = {}
        self._seen = set()  # idempotency keys already applied
        if self._path and self._path.exists():
            for line in self._path.read_text().splitlines():
                line = line.strip()
                if line:
                    self._apply(json.loads(line), persist=False)

    def _apply(self, entry, persist):
        key = entry.get('key')
        if key is not None and key in self._seen:
            return self._entries[-1] if self._entries else entry
        if entry['reason'] not in REASONS:
            raise LedgerError('Unknown ledger reason')
        delta = entry['delta']
        if not isinstance(delta, int) or isinstance(delta, bool):
            raise LedgerError('Credit amounts must be whole credits')
        balance = self._balances.get(entry['user'], 0)
        if balance + delta < 0:
            raise LedgerError('Insufficient credits')
        self._balances[entry['user']] = balance + delta
        self._entries.append(entry)
        if key is not None:
            self._seen.add(key)
        if persist and self._path:
            with self._path.open('a') as output:
                output.write(json.dumps(entry) + '\n')
        return entry

    def post(self, user, delta, reason, key=None, **meta):
        """Record one movement. `key` makes the post idempotent: a repeat with the
        same key is silently ignored, so webhook retries and settlement replays
        never double-count."""
        with self._lock:
            entry = {'ts': time.time(), 'user': str(user), 'delta': delta,
                     'reason': reason, 'key': key, **meta}
            return self._apply(entry, persist=True)

    def balance(self, user):
        with self._lock:
            return self._balances.get(str(user), 0)

    def balances(self):
        with self._lock:
            return dict(self._balances)

    def entries(self, user=None):
        with self._lock:
            if user is None:
                return list(self._entries)
            return [e for e in self._entries if e['user'] == str(user)]

    def applied(self, key):
        with self._lock:
            return key in self._seen
