import unittest
from research.capacity_selection_audit import replay_selection, return_stats, summarize_replay

class CapacitySelectionAuditTests(unittest.TestCase):
    def _c(self, sym, entry, exit_, r):
        return {
            "symbol": sym,
            "entry_date": entry,
            "exit_date": exit_,
            "net_return": r,
        }

    def test_same_day_exit_is_not_reused_at_open(self):
        rows = [
            self._c("A", "20260102", "20260105", 0.1),
            self._c("B", "20260105", "20260106", 0.1),
        ]
        x = replay_selection(rows, 1)
        self.assertEqual(len(x["selected"]), 1)
        self.assertEqual(len(x["skipped_no_slot"]), 1)

    def test_duplicate_symbol_is_skipped(self):
        rows = [
            self._c("A", "20260102", "20260110", 0.1),
            self._c("A", "20260105", "20260112", 0.2),
        ]
        x = replay_selection(rows, 10)
        self.assertEqual(len(x["selected"]), 1)
        self.assertEqual(len(x["skipped_duplicate"]), 1)

    def test_capacity_selection(self):
        rows = [
            self._c("A", "20260102", "20260110", 0.1),
            self._c("B", "20260102", "20260110", -0.1),
        ]
        x = summarize_replay(replay_selection(rows, 1), 2)
        self.assertEqual(x["counts"]["selected"], 1)
        self.assertEqual(x["counts"]["no_slot"], 1)
        self.assertAlmostEqual(x["coverage"], 0.5)

    def test_return_stats_profit_factor(self):
        s = return_stats([
            {"net_return": 0.2},
            {"net_return": -0.1},
            {"net_return": 0.1},
        ])
        self.assertAlmostEqual(s["profit_factor"], 3.0)

if __name__ == "__main__":
    unittest.main()
