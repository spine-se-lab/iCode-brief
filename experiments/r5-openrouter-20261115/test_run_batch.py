"""Offline checks for dispatch identity and valid grading."""
import unittest
from run_batch import dispatch_ready, valid_score


class SchedulerChecks(unittest.TestCase):
    def test_same_base_stays_serial(self):
        pending = [('a', 1), ('a', 2), ('b', 3), ('c', 4)]
        busy = set()
        self.assertEqual(dispatch_ready(pending, busy, 2, lambda item: item[0]), [('a', 1), ('b', 3)])
        self.assertEqual(pending, [('a', 2), ('c', 4)])
        self.assertEqual(dispatch_ready(pending, busy, 1, lambda item: item[0]), [('c', 4)])
        busy.remove('a')
        self.assertEqual(dispatch_ready(pending, busy, 1, lambda item: item[0]), [('a', 2)])

    def test_report_and_exit_required(self):
        result = {'exit_code': 0, 'score_revision': {'replay': 'verified', 'missing_reports': [], 'new_reward': {'reward': 1}}}
        self.assertTrue(valid_score(result))
        self.assertFalse(valid_score({**result, 'exit_code': 124}))
        self.assertFalse(valid_score({'exit_code': 0}))
        result['score_revision']['missing_reports'] = ['report.json']
        self.assertFalse(valid_score(result))


if __name__ == '__main__':
    unittest.main(verbosity=2)
