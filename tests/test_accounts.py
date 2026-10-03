import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from accounts import Accounts


class AccountsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.accounts = Accounts(Path(self.dir.name) / 'secret', Path(self.dir.name) / 'links.jsonl')

    def tearDown(self):
        self.dir.cleanup()

    def test_signed_token_round_trips(self):
        uid = self.accounts.mint()
        self.assertTrue(uid.startswith('u_'))
        self.assertEqual(self.accounts.verify(self.accounts.sign(uid)), uid)

    def test_tampered_or_forged_tokens_are_rejected(self):
        uid = self.accounts.mint()
        token = self.accounts.sign(uid)
        self.assertIsNone(self.accounts.verify(token[:-1] + ('0' if token[-1] != '0' else '1')))
        self.assertIsNone(self.accounts.verify('u_deadbeef.deadbeef'))  # forged signature
        self.assertIsNone(self.accounts.verify('u_nosig'))
        self.assertIsNone(self.accounts.verify(''))

    def test_secret_is_stable_across_instances(self):
        uid = self.accounts.mint()
        token = self.accounts.sign(uid)
        reopened = Accounts(Path(self.dir.name) / 'secret', Path(self.dir.name) / 'links.jsonl')
        self.assertEqual(reopened.verify(token), uid)  # same persisted secret verifies old cookies

    def test_customer_link_is_persisted_and_idempotent(self):
        uid = self.accounts.mint()
        self.accounts.link_customer(uid, 'cus_1', 'a@b.c')
        self.accounts.link_customer(uid, 'cus_1', 'a@b.c')
        self.assertEqual(self.accounts.customer_for(uid), 'cus_1')
        reopened = Accounts(Path(self.dir.name) / 'secret', Path(self.dir.name) / 'links.jsonl')
        self.assertEqual(reopened.customer_for(uid), 'cus_1')


if __name__ == '__main__':
    unittest.main()
