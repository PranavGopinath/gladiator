import copy
import unittest
from betting_simulation import DemoBook
from ledger import Ledger
from market import Market


class SimulationTests(unittest.TestCase):
    def state(self):
        return {'match_id': 'fixture', 'phase': 'running', 'started_at': 100, 'limit': 300,
                'players': {'a': {'alive': True}, 'b': {'alive': True}},
                'prediction': {'status': 'live', 'probabilities': {'a': .7, 'b': .3},
                               'factors': {'a': {'danger': .2}, 'b': {'danger': .2}}}}

    def test_bots_are_separate_and_settle_from_referee_result(self):
        real_ledger = Ledger(); real_ledger.post('human', 500, 'purchase')
        state = self.state(); real_market = Market(real_ledger); real_market.open('fixture', ['a', 'b'], draw=False)
        prediction = state['prediction']
        quote = real_market.quote(prediction, {'a', 'b'})
        book = DemoBook(seed=7)
        for now in range(100, 300, 7): book.update(state, {'winner': quote}, now)
        self.assertGreater(len(book.markets['winner'].bets), 10)
        self.assertGreater(len({b['outcome'] for b in book.markets['winner'].bets}), 1)
        self.assertEqual(real_ledger.balance('human'), 500)
        self.assertEqual(real_market.bets, [])
        frozen = copy.deepcopy(quote); frozen['status'] = 'void'; frozen['result'] = {'winning_outcome': 'draw'}
        state['phase'] = 'finished'
        book.update(state, {'winner': frozen}, 400)
        self.assertEqual(book.markets['winner'].status, 'void')
        self.assertEqual(book.markets['winner']._result['refunded'], sum(b['stake'] for b in book.markets['winner'].bets))
        before = len(book.markets['winner'].bets)
        book.update(state, {'winner': frozen}, 500)
        self.assertEqual(len(book.markets['winner'].bets), before)

    def test_demo_pays_even_when_real_pool_is_unfunded(self):
        state = self.state(); market = Market(Ledger()); market.open('fixture', ['a', 'b'], draw=False)
        q = market.quote(state['prediction'], {'a', 'b'})
        book = DemoBook(seed=7)
        book.update(state, {'winner': q}, 100)
        demo = book.markets['winner']
        book.fund('human'); demo.place_bet('human', 'a', 100)
        book.fund('other'); demo.place_bet('other', 'b', 100)
        q['status'] = 'void'; q['result'] = {'winning_outcome': 'a', 'reason': 'No opposing pool'}
        state['phase'] = 'finished'
        book.update(state, {'winner': q}, 400)
        self.assertEqual(demo.status, 'settled')
        self.assertEqual(demo._result['winning_outcome'], 'a')

    def test_preparation_does_not_prevent_starting_bots(self):
        state = self.state(); state['phase'] = 'preparing'
        book = DemoBook(seed=2); book.update(state, {}, 100)
        state['phase'] = 'running'
        market = Market(Ledger()); market.open('fixture', ['a', 'b'])
        book.update(state, {'winner': market.quote(None, {'a', 'b'})}, 101)
        self.assertIn('winner', book.markets)
