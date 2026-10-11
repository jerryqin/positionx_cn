import unittest

import numpy as np
import pandas as pd

from research.trend_edge_scan import (
    FEATURES,
    HORIZONS,
    FOLDS,
    add_stock_features,
    attach_forward_excess,
    assign_quintiles,
    fast_reject,
    history_integrity,
    quarantine_histories,
)


class TrendEdgeScanV02Tests(unittest.TestCase):
    def test_frozen_spec_unchanged(self):
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

    def test_exact_t1_open_s20_close_label(self):
        dates = [f"202601{i:02d}" for i in range(1, 26)]
        stock = pd.DataFrame(
            {
                "open": [100.0]*25,
                "close": [100.0]*25,
            },
            index=dates,
        )
        bench = stock.copy()
        # Signal at index 0; T+1 open = 100; S20 close = 110.
        stock.loc[dates[20], "close"] = 110.0
        x = attach_forward_excess(stock, bench, dates, (20,))
        self.assertAlmostEqual(x.loc[dates[0], "excess_20"], 0.10)

    def test_corrupt_adjusted_gap_is_quarantined(self):
        dates = ["20260101","20260102","20260103"]
        bad = pd.DataFrame(
            {
                "open":[10.0, 10.0, 1000000.0],
                "close":[10.0, 10.0, 1000000.0],
            },
            index=dates,
        )
        audit = history_integrity(bad)
        self.assertFalse(audit["ok"])
        good, quarantined = quarantine_histories({"X":bad})
        self.assertNotIn("X", good)
        self.assertEqual(quarantined[0]["symbol"], "X")

    def test_clean_history_is_not_quarantined(self):
        dates = pd.date_range("2026-01-01", periods=100, freq="D")
        close = pd.Series(np.linspace(10, 15, 100), index=dates)
        x = pd.DataFrame(
            {"open":close*0.999, "close":close},
            index=[d.strftime("%Y%m%d") for d in dates],
        )
        self.assertTrue(history_integrity(x)["ok"])

    def test_quintiles_still_cross_sectional(self):
        x = pd.Series(range(100), dtype=float)
        q = assign_quintiles(x)
        self.assertEqual(
            q.value_counts().to_dict(),
            {"Q1":20,"Q2":20,"Q3":20,"Q4":20,"Q5":20},
        )

    def test_fast_reject_gate_unchanged(self):
        full = {
            str(h): {"q5_minus_q1":0.01, "monotonicity":0.9}
            for h in HORIZONS
        }
        folds = {
            f: {
                str(h): {"q5_minus_q1":0.01}
                for h in HORIZONS
            }
            for f in ("F1","F2","F3")
        }
        r = fast_reject({"full_sample":full, "folds":folds})
        self.assertTrue(r["fast_reject_candidate"])


if __name__ == "__main__":
    unittest.main()
