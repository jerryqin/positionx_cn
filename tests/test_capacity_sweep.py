import unittest
from research.capacity_sweep import classify_capacity_shape, extract_capacity_point, normalize_capacities

class CapacitySweepTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_capacities([30, 10, 20, 10]), [10, 20, 30])

    def test_extract(self):
        s = {
            "signals_executable": 100,
            "trades_executed": 60,
            "skipped_no_slot": 35,
            "skipped_duplicate_symbol": 5,
            "total_return": 0.1,
            "cagr_calendar_basis": 0.02,
            "max_drawdown": -0.2,
            "sharpe_actual_sessions_including_first_day": 0.3,
            "avg_exposure": 0.8,
            "avg_active_positions": 8,
            "max_concurrent_positions": 10,
        }
        p = extract_capacity_point(10, s)
        self.assertAlmostEqual(p["execution_coverage"], 0.60)
        self.assertAlmostEqual(p["capacity_skip_rate"], 0.35)
        self.assertAlmostEqual(p["total_skip_rate"], 0.40)

    def test_shape(self):
        pts = [
            {"max_positions": 10, "capacity_skip_rate": 0.60, "total_return": -0.2},
            {"max_positions": 20, "capacity_skip_rate": 0.18, "total_return": -0.05},
            {"max_positions": 30, "capacity_skip_rate": 0.08, "total_return": 0.03},
            {"max_positions": 40, "capacity_skip_rate": 0.03, "total_return": 0.04},
        ]
        x = classify_capacity_shape(pts)
        self.assertEqual(x["first_capacity_below_20pct_no_slot"], 20)
        self.assertEqual(x["first_capacity_below_10pct_no_slot"], 30)
        self.assertEqual(x["first_capacity_below_5pct_no_slot"], 40)
        self.assertEqual(x["first_positive_total_return_capacity"], 30)

if __name__ == "__main__":
    unittest.main()
