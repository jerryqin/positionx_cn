import unittest
import numpy as np
import pandas as pd

from research.trend_edge_scan import (
    HORIZONS,
    attach_forward_excess,
    fast_reject,
    history_integrity,
    quarantine_histories_with_audit,
    sanitize_history_prices,
)


class TrendEdgeScanV021CompatibilityTests(unittest.TestCase):
    def test_one_missing_endpoint_is_allowed(self):
        x = pd.DataFrame(
            {"open":[10.0,np.nan,10.5], "close":[10.0,10.2,10.6]},
            index=["20260101","20260102","20260103"],
        )
        self.assertTrue(history_integrity(x)["ok"])

    def test_paired_zero_suspension_row_is_sanitized(self):
        x = pd.DataFrame(
            {"open":[10.0,0.0,10.2], "close":[10.0,0.0,10.3]},
            index=["20260101","20260102","20260103"],
        )
        clean, info = sanitize_history_prices(x)
        self.assertEqual(info["paired_zero_rows_sanitized"], 1)
        self.assertTrue(pd.isna(clean.loc["20260102","open"]))

    def test_quarantine_extended_audit(self):
        x = pd.DataFrame(
            {"open":[10.0,0.0,10.2], "close":[10.0,0.0,10.3]},
            index=["20260101","20260102","20260103"],
        )
        good, bad, audit = quarantine_histories_with_audit({"X":x})
        self.assertIn("X", good)
        self.assertEqual(bad, [])
        self.assertEqual(audit[0]["symbol"], "X")

    def test_exact_label_and_fast_reject_still_work(self):
        dates = [f"202601{i:02d}" for i in range(1,26)]
        stock = pd.DataFrame({"open":[100.0]*25,"close":[100.0]*25}, index=dates)
        bench = stock.copy()
        stock.loc[dates[20],"close"] = 110.0
        y = attach_forward_excess(stock, bench, dates, (20,))
        self.assertAlmostEqual(y.loc[dates[0],"excess_20"], 0.10)

        full = {str(h):{"q5_minus_q1":0.01,"monotonicity":0.9} for h in HORIZONS}
        folds = {f:{str(h):{"q5_minus_q1":0.01} for h in HORIZONS} for f in ("F1","F2","F3")}
        self.assertTrue(fast_reject({"full_sample":full,"folds":folds})["fast_reject_candidate"])


if __name__ == "__main__":
    unittest.main()
