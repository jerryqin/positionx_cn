import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "research/forward_ret120.py"
spec = importlib.util.spec_from_file_location("forward_ret120_test", MOD)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


class Ret120ForwardTests(unittest.TestCase):
    def test_policy_frozen_constants(self):
        p = m.Ret120ForwardPolicy()
        self.assertEqual(p.strategy_id, "RET120_CSI500_FORWARD_V1_0")
        self.assertEqual(p.historical_cutoff, "20260930")
        self.assertEqual(p.dividend_type, "back_ratio")
        self.assertEqual(p.feature_lookback_sessions, 120)
        self.assertEqual(p.target_weight, 0.01)
        self.assertEqual(p.max_target_gross, 0.30)
        self.assertEqual(p.max_positions, 30)
        self.assertEqual(p.completed_trades_target, 60)
        self.assertEqual(p.review_calendar_months, 12)

    def test_ret120_exact_shift_120(self):
        dates = pd.bdate_range("2025-01-01", periods=130)
        df = pd.DataFrame({"close": range(100, 230)}, index=dates)
        x = m.normalize_history(df)
        d = x.index[120]
        r, reason = m.ret120_with_reason(x, d)
        self.assertEqual(reason, "OK")
        self.assertAlmostEqual(r, 220/100-1, places=12)

    def test_qcut_ties_follow_symbol_asc_rank_first(self):
        dates = pd.bdate_range("2025-01-01", periods=121)
        members = [f"{i:06d}.SZ" for i in range(500)]
        histories = {}
        for s in members:
            histories[s] = m.normalize_history(pd.DataFrame({"close": [100.0]*121}, index=dates))
        d = dates[-1].strftime("%Y%m%d")
        snap = m.build_daily_snapshot(histories, members, d, 400)
        q5 = snap[snap.selected_q5].symbol.tolist()
        self.assertEqual(len(q5), 100)
        self.assertEqual(q5, sorted(members)[400:])

    def test_min_coverage_fail_closed(self):
        dates = pd.bdate_range("2025-01-01", periods=121)
        members = [f"{i:06d}.SZ" for i in range(500)]
        histories = {s: m.normalize_history(pd.DataFrame({"close": [100.0]*121}, index=dates)) for s in members[:399]}
        with self.assertRaises(ValueError):
            m.build_daily_snapshot(histories, members, dates[-1].strftime("%Y%m%d"), 400)

    def test_standard_open_v1_blocks_up_limit_and_suspension(self):
        self.assertFalse(m.standard_open_v1(10, 10, 0, 0.01)["fillable"])
        self.assertFalse(m.standard_open_v1(9.9, 10, 1, 0.01)["fillable"])
        self.assertTrue(m.standard_open_v1(9.9, 10, 0, 0.01)["fillable"])

    def test_standard_open_v1_no_limit_one_price_lock(self):
        self.assertFalse(m.standard_open_v1(10, None, None, high_price=10, low_price=10, close_price=10, prev_close=9)["fillable"])
        self.assertTrue(m.standard_open_v1(10, None, None, high_price=10.2, low_price=9.9, close_price=10, prev_close=9)["fillable"])

    def test_s120_defer_rule(self):
        self.assertEqual(m.exit_action(119, None), "HOLD")
        self.assertEqual(m.exit_action(120, None), "DEFER_NO_VALID_CLOSE")
        self.assertEqual(m.exit_action(121, None), "DEFER_NO_VALID_CLOSE")
        self.assertEqual(m.exit_action(121, 12.3), "EXIT_AT_CLOSE")

    def test_review_gate_date(self):
        self.assertEqual(m.review_not_before("20261008", 12), "20271008")

    def test_new_listing_special_semantics(self):
        cal = ["20261008", "20261009", "20261012", "20261013", "20261014", "20261015"]
        self.assertTrue(m.new_listing_special("600001.SH", "20261008", "20261008", cal))
        self.assertFalse(m.new_listing_special("600001.SH", "20261008", "20261009", cal))
        self.assertTrue(m.new_listing_special("300001.SZ", "20261008", "20261014", cal))
        self.assertFalse(m.new_listing_special("300001.SZ", "20261008", "20261015", cal))

    def test_runner_chronology_and_no_broker(self):
        src = (ROOT / "scripts/run_ret120_csi500_forward.py").read_text(encoding="utf-8")
        a = src.index("entry_diag = process_entries")
        b = src.index("exit_diag = process_exits_and_mark")
        c = src.index("signal_diag = detect_signals")
        self.assertLess(a, b)
        self.assertLess(b, c)
        self.assertNotIn("xttrader", src.lower())
        self.assertNotIn("order_stock", src.lower())

    def test_snapshot_hash_is_stable(self):
        df = pd.DataFrame([
            {"signal_date":"20261008","symbol":"000001.SZ","ret120":0.1,"rank_first":1.0,"quintile":"Q1","selected_q5":False,"feature_valid":True,"feature_reason":"OK"},
            {"signal_date":"20261008","symbol":"000002.SZ","ret120":0.2,"rank_first":2.0,"quintile":"Q5","selected_q5":True,"feature_valid":True,"feature_reason":"OK"},
        ])
        self.assertEqual(m.snapshot_sha256(df), m.snapshot_sha256(df.iloc[::-1]))


if __name__ == "__main__":
    unittest.main()
