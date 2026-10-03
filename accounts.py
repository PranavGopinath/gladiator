"""Spectator identity for the betting market.

A bettor is identified by a server-issued, HMAC-signed session cookie — the client
never supplies its own user id, so one spectator cannot spend another's credits by
naming their id. The signing secret is generated once and kept host-side (mode
600). When a spectator buys credits, their session id is linked to the Stripe
Customer created at checkout, so credits persist to a real payer; that mapping is
the foundation for optional cross-device (magic-link) login later.
"""
import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path


class Accounts:
    def __init__(self, secret_path, link_path=None):
        self._lock = threading.RLock()
        self._secret_path = Path(secret_path)
        self._link_path = Path(link_path) if link_path else None
        self._secret = self._load_secret()
        self._links = {}
        if self._link_path and self._link_path.exists():
            for line in self._link_path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    self._links[row['uid']] = row

    def _load_secret(self):
        try:
            return self._secret_path.read_bytes()
        except OSError:
            secret = secrets.token_bytes(32)
            self._secret_path.write_bytes(secret)
            try:
                self._secret_path.chmod(0o600)
            except OSError:
                pass
            return secret

    def mint(self):
        return 'u_' + secrets.token_hex(8)

    def _mac(self, uid):
        return hmac.new(self._secret, uid.encode(), hashlib.sha256).hexdigest()

    def sign(self, uid):
        return f'{uid}.{self._mac(uid)}'

    def verify(self, token):
        """Return the uid if the cookie is a valid signature, else None."""
        if not token or token.count('.') != 1:
            return None
        uid, signature = token.split('.', 1)
        if uid.startswith('u_') and hmac.compare_digest(signature, self._mac(uid)):
            return uid
        return None

    def link_customer(self, uid, customer, email=None):
        """Associate a session id with its Stripe Customer (append-only, idempotent)."""
        if not uid or not customer:
            return
        with self._lock:
            if self._links.get(uid, {}).get('customer') == customer:
                return
            row = {'uid': uid, 'customer': customer, 'email': email, 'ts': time.time()}
            self._links[uid] = row
            if self._link_path:
                with self._link_path.open('a') as output:
                    output.write(json.dumps(row) + '\n')

    def customer_for(self, uid):
        with self._lock:
            return self._links.get(uid, {}).get('customer')
