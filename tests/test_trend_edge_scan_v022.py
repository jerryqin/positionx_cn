import unittest

import numpy as np
import pandas as pd

from research.trend_edge_scan import (
    FEATURES,
    HORIZONS,
    FOLDS,
    PRICE_ADJUSTMENT_MODE,
    attach_forward_excess,
    assign_quintiles,
    fast_reject,
    history_integrity,
    quarantine_histories,
    quarantine_histories_with_audit,
    sanitize_history_prices,
)


class TrendEdgeScanV022Tests(unittest.TestCase):
    def test_frozen_economic_spec_unchanged(self):
        self.assertEqual(
            FEATURES,
            (
                "ret20","ret60","ret120",
                "rs20","rs60",
                "dist_ma20","dist_ma60","drawdown20",
            ),
        )
        self.assertEqual(HORIZONS, (20,60,120))
        self.assertEqual(
            FOLDS,
            (
                ("F1","20200803","20221017"),
                ("F2","20221018","20240919"),
                ("F3","20240920","20260924"),
            ),
        )

    def test_price_representation_is_back_ratio(self):
        self.assertEqual(PRICE_ADJUSTMENT_MODE, "back_ratio")

    def test_legacy_quarantine_api_still_returns_two_values(self):
        x = pd.DataFrame(
            {"open":[10.0, 10.1], "close":[10.0, 10.2]},
            index=["20260101","20260102"],
        )
        good, bad = quarantine_histories({"X":x})
        self.assertIn("X", good)
        self.assertEqual(bad, [])

    def test_extended_quarantine_api_returns_sanitation_audit(self):
        x = pd.DataFrame(
            {"open":[10.0, 0.0, 10.2], "close":[10.0, 0.0, 10.3]},
            index=["20260101","20260102","20260103"],
        )
        good, bad, audit = quarantine_histories_with_audit({"X":x})
        self.assertIn("X", good)
        self.assertEqual(bad, [])
        self.assertEqual(audit[0]["paired_zero_rows_sanitized"], 1)

    def test_negative_adjusted_level_is_rejected_if_it_somehow_survives_back_ratio(self):
        x = pd.DataFrame(
            {"open":[10.0, -1.0], "close":[10.0, -1.0]},
            index=["20260101","20260102"],
        )
        self.assertFalse(history_integrity(x)["ok"])

    def test_absurd_gap_still_rejected(self):
        x = pd.DataFrame(
            {"open":[10.0, np.nan, 1000.0], "close":[10.0, np.nan, 1000.0]},
            index=["20260101","20260102","20260103"],
        )
        a = history_integrity(x)
        self.assertFalse(a["ok"])
        self.assertIn(a["reason"], {
            "ABSURD_ADJUSTED_CLOSE_GAP",
            "ABSURD_ADJUSTED_OPEN_GAP",
        })

    def test_exact_t1_open_s20_close_label_unchanged(self):
        dates = [f"202601{i:02d}" for i in range(1, 26)]
        stock = pd.DataFrame(
            {"open":[100.0]*25, "close":[100.0]*25},
            index=dates,
        )
        bench = stock.copy()
        stock.loc[dates[20], "close"] = 110.0
        y = attach_forward_excess(stock, bench, dates, (20,))
        self.assertAlmostEqual(y.loc[dates[0],"excess_20"], 0.10)

    def test_fast_reject_gate_unchanged(self):
        full = {
            str(h): {"q5_minus_q1":0.01, "monotonicity":0.9}
            for h in HORIZONS
        }
        folds = {
            f: {str(h): {"q5_minus_q1":0.01} for h in HORIZONS}
            for f in ("F1","F2","F3")
        }
        r = fast_reject({"full_sample":full, "folds":folds})
        self.assertTrue(r["fast_reject_candidate"])


if __name__ == "__main__":
    unittest.main()
