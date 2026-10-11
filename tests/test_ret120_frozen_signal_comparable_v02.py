from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

spec0 = importlib.util.spec_from_file_location(
    "audit_ret120_frozen_signal_lineage_v01",
    SCRIPTS / "audit_ret120_frozen_signal_lineage_v01.py",
)
p0 = importlib.util.module_from_spec(spec0)
assert spec0 and spec0.loader
spec0.loader.exec_module(p0)

import sys
import types
sys.modules["audit_ret120_frozen_signal_lineage_v01"] = p0
try:
    import duckdb  # noqa: F401
except ModuleNotFoundError:
    sys.modules["duckdb"] = types.ModuleType("duckdb")

spec = importlib.util.spec_from_file_location(
    "p1",
    SCRIPTS / "audit_ret120_frozen_signal_comparable_v02.py",
)
p1 = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(p1)


class FrozenSignalComparableV02Tests(unittest.TestCase):
    def test_frozen_quintile_order_differs_from_value_symbol_resort_when_tied(self):
        # Frozen v0.2.2 rank(method='first') respects panel row order for ties.
        v = pd.Series([1.0] * 100)
        q = p0.assign_quintiles(v)
        self.assertTrue((q.iloc[:20] == "Q1").all())
        self.assertTrue((q.iloc[-20:] == "Q5").all())

    def test_frozen_bucket_normalizer_preserves_legitimate_missing_rows(self):
        x = pd.Series(["Q1", "Q5", np.nan, None, "Q3"], dtype="object")
        y = p1.normalize_frozen_bucket_labels(x)
        self.assertEqual(str(y.dtype), "Int64")
        self.assertEqual(int(y.iloc[0]), 1)
        self.assertEqual(int(y.iloc[1]), 5)
        self.assertTrue(pd.isna(y.iloc[2]))
        self.assertTrue(pd.isna(y.iloc[3]))
        self.assertEqual(int(y.iloc[4]), 3)

    def test_frozen_bucket_normalizer_rejects_unknown_nonnull_label(self):
        with self.assertRaisesRegex(RuntimeError, "unexpected frozen bucket label"):
            p1.normalize_frozen_bucket_labels(pd.Series(["Q1", "Q6", np.nan]))

    def test_portfolio_constants_match_52wh_policy(self):
        self.assertEqual(p1.TARGET_WEIGHT, 0.01)
        self.assertEqual(p1.MAX_GROSS, 0.30)
        self.assertEqual(p1.MAX_POSITIONS, 30)
        self.assertEqual(p1.MAIN_COST, 0.002)
        self.assertEqual(p1.STRESS_COST, 0.005)
        self.assertEqual(p1.RANDOM_SEEDS, 100)

    def test_add_timing_is_t_plus_1_and_s120(self):
        cal = [f"D{i:03d}" for i in range(130)]
        x = pd.DataFrame([{
            "signal_date": "D000", "symbol": "000001.SZ",
            "ret120": 0.2, "bucket": 5
        }])
        y = p1.add_timing(x, cal)
        self.assertEqual(y.iloc[0]["entry_date"], "D001")
        self.assertEqual(y.iloc[0]["exit_date"], "D120")

    def test_same_day_exit_not_reusable_at_open_semantic_is_preserved(self):
        # Static source-level guard against accidentally moving exits before entries.
        src = (SCRIPTS / "audit_ret120_frozen_signal_comparable_v02.py").read_text(encoding="utf-8")
        self.assertLess(src.index("remaining_gross ="), src.index("# mark close before exact exits"))
        self.assertLess(src.index("selected = eligible[:slots]"), src.index("# mark close before exact exits"))

    def test_signal_recompute_from_canonical_is_not_in_run_path(self):
        src = (SCRIPTS / "audit_ret120_frozen_signal_comparable_v02.py").read_text(encoding="utf-8")
        run = src[src.index("def run() -> int:"):src.index("def main() -> int:")]
        self.assertIn("load_frozen_signal_events", run)
        self.assertNotIn("assign_quintiles(", run)


if __name__ == "__main__":
    unittest.main()
