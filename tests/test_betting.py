import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ledger import Ledger, LedgerError
from market import Market, price_for, _apportion, PRICE_FLOOR


def live(**probs):
    return {'status': 'live', 'probabilities': probs}


class LedgerTests(unittest.TestCase):
    def test_balance_is_the_sum_of_entries(self):
        ledger = Ledger()
        ledger.post('a', 500, 'purchase')
        ledger.post('a', -200, 'bet')
        ledger.post('a', 350, 'payout')
        self.assertEqual(ledger.balance('a'), 650)

    def test_overdraw_is_rejected_and_leaves_balance_intact(self):
        ledger = Ledger()
        ledger.post('a', 100, 'purchase')
        with self.assertRaises(LedgerError):
            ledger.post('a', -101, 'bet')
        self.assertEqual(ledger.balance('a'), 100)

    def test_non_integer_credits_are_rejected(self):
        ledger = Ledger()
        with self.assertRaises(LedgerError):
            ledger.post('a', 10.5, 'purchase')

    def test_idempotency_key_prevents_double_credit(self):
        ledger = Ledger()
        ledger.post('a', 500, 'purchase', key='stripe-evt-1')
        ledger.post('a', 500, 'purchase', key='stripe-evt-1')
        self.assertEqual(ledger.balance('a'), 500)
        self.assertTrue(ledger.applied('stripe-evt-1'))

    def test_entries_survive_a_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ledger.jsonl'
            first = Ledger(path)
            first.post('a', 500, 'purchase', key='k1')
            first.post('a', -200, 'bet', key='k2')
            reloaded = Ledger(path)
            self.assertEqual(reloaded.balance('a'), 300)
            # The replayed keys are still recognised as applied.
            reloaded.post('a', 500, 'purchase', key='k1')
            self.assertEqual(reloaded.balance('a'), 300)


class PricingTests(unittest.TestCase):
    def test_jev_live_probability_is_used_when_present(self):
        self.assertAlmostEqual(price_for('codex', live(codex=.7, claude=.3), {'codex': 0, 'claude': 0}), .7)

    def test_pool_implied_price_when_jev_absent(self):
        pools = {'codex': 300, 'claude': 100, 'draw': 0}
        self.assertAlmostEqual(price_for('codex', None, pools), .75)

    def test_uniform_prior_on_empty_pool_without_jev(self):
        self.assertAlmostEqual(price_for('codex', None, {'codex': 0, 'claude': 0}), .5)

    def test_draw_is_always_pool_implied_even_with_live_jev(self):
        # Jev never quotes a draw, so a live forecast must not price it to zero.
        price = price_for('draw', live(codex=.6, claude=.4), {'codex': 60, 'claude': 40, 'draw': 0})
        self.assertEqual(price, PRICE_FLOOR)

    def test_price_floor_clamps_tiny_probabilities(self):
        self.assertEqual(price_for('codex', live(codex=.0001, claude=.9999), {'codex': 0, 'claude': 0}), PRICE_FLOOR)

    def test_apportion_loses_no_credit(self):
        shares = _apportion(100, {'a': 1, 'b': 1, 'c': 1})
        self.assertEqual(sum(shares.values()), 100)


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()
        for user in ('early', 'late', 'loser'):
            self.ledger.post(user, 1000, 'purchase')
        self.market = Market(self.ledger)
        self.market.open('m1', ['codex', 'claude'])

    def test_bet_debits_credits_and_grows_the_pool(self):
        self.market.place_bet('early', 'codex', 100, prediction=live(codex=.5, claude=.5))
        self.assertEqual(self.ledger.balance('early'), 900)
        self.assertEqual(self.market.pools['codex'], 100)

    def test_insufficient_credits_leaves_the_pool_untouched(self):
        with self.assertRaises(LedgerError):
            self.market.place_bet('early', 'codex', 5000)
        self.assertEqual(self.market.pools['codex'], 0)

    def test_closed_outcome_is_rejected_by_the_referee_gate(self):
        # codex has been eliminated; only claude and draw remain open.
        with self.assertRaises(Exception):
            self.market.place_bet('late', 'codex', 100, open_outcomes={'claude', 'draw'})

    def test_snapshot_weighting_rewards_the_earlier_conviction_bet(self):
        # Same stake on the eventual winner, but 'early' bet when codex looked
        # unlikely (low price) and 'late' bet when codex was the favourite.
        self.market.place_bet('early', 'codex', 100, prediction=live(codex=.2, claude=.8))
        self.market.place_bet('late', 'codex', 100, prediction=live(codex=.8, claude=.2))
        self.market.place_bet('loser', 'claude', 200, prediction=live(codex=.8, claude=.2))
        result = self.market.settle('codex', 'winner')
        self.assertEqual(result['status'], 'settled')
        self.assertEqual(result['distributed'], 400)  # whole pool paid out, zero rake
        self.assertGreater(result['payouts']['early'], result['payouts']['late'])
        self.assertNotIn('loser', result['payouts'])

    def test_payouts_sum_to_the_pool_and_settlement_is_idempotent(self):
        self.market.place_bet('early', 'codex', 100)
        self.market.place_bet('loser', 'claude', 300)
        first = self.market.settle('codex', 'winner')
        self.assertEqual(first['distributed'], 400)
        self.assertEqual(self.ledger.balance('early'), 1300)  # 900 + 400
        second = self.market.settle('codex', 'winner')
        self.assertEqual(first, second)
        self.assertEqual(self.ledger.balance('early'), 1300)  # not paid twice

    def test_rake_is_withheld_from_the_distribution(self):
        market = Market(self.ledger, rake_bps=500)  # 5%
        market.open('m2', ['codex', 'claude'])
        market.place_bet('early', 'codex', 100)
        market.place_bet('loser', 'claude', 300)
        result = market.settle('codex', 'winner')
        self.assertEqual(result['rake'], 20)
        self.assertEqual(result['distributed'], 380)

    def test_one_sided_pool_is_voided_and_refunded(self):
        self.market.place_bet('early', 'codex', 100)
        self.market.place_bet('late', 'codex', 100)
        result = self.market.settle('codex', 'winner')
        self.assertEqual(result['status'], 'void')
        self.assertEqual(self.ledger.balance('early'), 1000)
        self.assertEqual(self.ledger.balance('late'), 1000)

    def test_no_winning_bets_refunds_everyone(self):
        self.market.place_bet('loser', 'claude', 200)
        self.market.place_bet('late', 'claude', 100)
        result = self.market.settle('codex', 'winner')
        self.assertEqual(result['status'], 'void')
        self.assertEqual(self.ledger.balance('loser'), 1000)

    def test_canceled_match_voids_and_refunds(self):
        self.market.place_bet('early', 'codex', 100)
        self.market.place_bet('loser', 'claude', 300)
        result = self.market.settle(None, 'canceled')
        self.assertEqual(result['status'], 'void')
        self.assertEqual(self.ledger.balance('early'), 1000)
        self.assertEqual(self.ledger.balance('loser'), 1000)

    def test_draw_outcome_pays_the_draw_pool(self):
        self.market.place_bet('early', 'draw', 100)
        self.market.place_bet('loser', 'codex', 300)
        result = self.market.settle(None, 'draw')
        self.assertEqual(result['winning_outcome'], 'draw')
        self.assertEqual(self.ledger.balance('early'), 1300)


class MultiMarketTests(unittest.TestCase):
    def test_two_markets_on_one_match_do_not_share_ledger_keys(self):
        ledger = Ledger()
        for user in ('x', 'y'):
            ledger.post(user, 1000, 'purchase')
        winner = Market(ledger, name='winner'); first = Market(ledger, name='first_blood')
        winner.open('m', ['a', 'b']); first.open('m', ['a', 'b', 'nobody'], draw=False)
        winner.place_bet('x', 'a', 100); winner.place_bet('y', 'b', 100)
        first.place_bet('x', 'a', 100); first.place_bet('y', 'b', 100)
        winner.settle('a', 'winner'); first.settle('a', 'winner')
        # x wins both pools: 1000 - 200 + 200 + 200.
        self.assertEqual(ledger.balance('x'), 1200)
        self.assertEqual(ledger.balance('y'), 800)

    def test_position_projects_and_then_reports_the_actual_result(self):
        ledger = Ledger()
        for user in ('x', 'y'):
            ledger.post(user, 1000, 'purchase')
        market = Market(ledger)
        market.open('m', ['a', 'b'])
        market.place_bet('x', 'a', 100, prediction=live(a=.25, b=.75))
        market.place_bet('y', 'a', 100, prediction=live(a=.5, b=.5))
        market.place_bet('y', 'b', 200)
        [row] = market.position('x')
        self.assertEqual(row['status'], 'live'); self.assertEqual(row['odds'], 4.0)
        # x holds weight 400 of 600 on 'a' against a 400 pool.
        self.assertEqual(row['projected'], 266)
        quote = market.quote()
        self.assertAlmostEqual(quote['outcomes']['a']['weight'], 600)
        market.settle('b', 'winner')
        [row] = market.position('x')
        self.assertEqual((row['status'], row['returned']), ('lost', 0))
        rows = {r['outcome']: r for r in market.position('y')}
        self.assertEqual((rows['b']['status'], rows['b']['returned']), ('won', 400))

    def test_void_reports_refunds_in_positions(self):
        ledger = Ledger(); ledger.post('x', 1000, 'purchase')
        market = Market(ledger); market.open('m', ['a', 'b'])
        market.place_bet('x', 'a', 100)
        market.settle(None, 'canceled')
        [row] = market.position('x')
        self.assertEqual((row['status'], row['returned']), ('refunded', 100))


if __name__ == '__main__':
    unittest.main()
