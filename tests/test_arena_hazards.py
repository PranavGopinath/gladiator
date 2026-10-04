import unittest
from arena_hazards import Hazards, RESET


class HazardTests(unittest.TestCase):
    def test_warning_then_reset_every_thirty_seconds(self):
        h = Hazards('match')
        alive = ['agent-1', 'agent-2']
        self.assertIsNone(h.tick(19.9, alive))
        warning = h.tick(20, alive)
        self.assertEqual(warning['action'], 'warning')
        self.assertIsNone(h.tick(29.9, alive))
        pulse = h.tick(30, alive)
        self.assertEqual(pulse, {**warning, 'action': 'disruption'})
        self.assertEqual(h.tick(50, alive)['at'], 60)
        self.assertEqual(h.tick(60, alive)['action'], 'disruption')

    def test_target_dying_skips_reset(self):
        h = Hazards('match')
        warning = h.tick(20, ['a', 'b', 'c'])
        remaining = [p for p in ['a', 'b', 'c'] if p != warning['target']]
        self.assertEqual(h.tick(30, remaining)['action'], 'skipped')

    def test_seed_reproduces_target_sequence(self):
        a, b = Hazards('match'), Hazards('match')
        for t in [20, 30, 50, 60, 80, 90]:
            self.assertEqual(a.tick(t, ['a', 'b']), b.tick(t, ['b', 'a']))

    def test_no_pulses_with_one_survivor_or_catchup_burst(self):
        h = Hazards('match')
        self.assertIsNone(h.tick(20, ['a']))
        self.assertEqual(h.tick(95, ['a', 'b'])['action'], 'skipped')
        self.assertIsNone(h.tick(96, ['a', 'b']))
        self.assertEqual(h.tick(110, ['a', 'b'])['at'], 120)

    def test_reset_script_compiles(self):
        compile(RESET, '<hazard-reset>', 'exec')
