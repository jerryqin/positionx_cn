import tempfile
import unittest
from pathlib import Path

import pandas as pd

from research.fillability_gate import (
    FillabilityConfig,
    apply_fillability_filter,
    classify_entry,
    load_event_status_rows,
)


def base_row(**kw):
    r = {
        "trade_date": pd.Timestamp("2025-01-02"),
        "board": "MAIN",
        "exchange": "SSE",
        "is_suspended": False,
        "pre_close": 10.0,
        "open": 10.2,
        "high": 10.4,
        "low": 10.1,
        "close": 10.3,
        "up_limit": 11.0,
        "down_limit": 9.0,
        "is_one_price_up_limit": False,
        "is_open_at_up_limit": False,
        "can_buy_at_open": True,
        "st_status": "NORMAL",
        "st_known": True,
        "limit_ratio": 0.10,
        "limit_ratio_source": "BOARD_DEFAULT",
        "new_listing_special": False,
    }
    r.update(kw)
    return r


class FillabilityGateTests(unittest.TestCase):
    def test_suspended_is_blocked(self):
        ok, reason, _ = classify_entry(base_row(is_suspended=True), FillabilityConfig())
        self.assertFalse(ok)
        self.assertEqual(reason, "SUSPENDED")

    def test_open_at_limit_is_blocked(self):
        ok, reason, _ = classify_entry(
            base_row(open=11.0, high=11.0, low=10.8, close=11.0,
                     is_open_at_up_limit=True, can_buy_at_open=False),
            FillabilityConfig(),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "OPEN_AT_UP_LIMIT")

    def test_conservative_unknown_st_blocks_hypothetical_five_percent_limit(self):
        ok, reason, extra = classify_entry(
            base_row(st_known=False, open=10.5, high=10.8, close=10.6),
            FillabilityConfig(conservative_unknown_st=True),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "UNKNOWN_MAIN_ST_AT_OR_ABOVE_5PCT")
        self.assertEqual(extra["hypothetical_st_up_limit"], 10.5)

    def test_standard_unknown_st_does_not_overblock(self):
        ok, reason, _ = classify_entry(
            base_row(st_known=False, open=10.5, high=10.8, close=10.6),
            FillabilityConfig(conservative_unknown_st=False),
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "FILLABLE_AT_OPEN")

    def test_limit_surface_inconsistency_is_blocked(self):
        ok, reason, _ = classify_entry(
            base_row(high=11.1, close=10.9), FillabilityConfig()
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "LIMIT_DATA_INCONSISTENT")

    def test_parquet_join_and_missing_row_fail_closed(self):
        try:
            import duckdb
        except ImportError:
            self.skipTest("duckdb not installed in isolated build environment")
        with tempfile.TemporaryDirectory() as td:
            pq = Path(td) / "s.parquet"
            row = base_row()
            df = pd.DataFrame([{
                "trade_date": row["trade_date"], "symbol": "600000.SH",
                **{k:v for k,v in row.items() if k != "trade_date"},
            }])
            con = duckdb.connect()
            con.register("x", df)
            con.execute("COPY x TO ? (FORMAT PARQUET)", [str(pq)])
            con.close()

            events = [
                {"symbol":"600000.SH","entry_date":"20250102","signal_date":"20250101","strategy":"BREAKOUT_RETEST","return_20d":0.1},
                {"symbol":"600001.SH","entry_date":"20250102","signal_date":"20250101","strategy":"BREAKOUT_RETEST","return_20d":0.2},
            ]
            status = load_event_status_rows(events, pq)
            kept, audit = apply_fillability_filter(events, status, FillabilityConfig())
            self.assertEqual(len(kept), 1)
            self.assertEqual(audit["reason_counts"]["MISSING_STATUS_ROW"], 1)


if __name__ == "__main__":
    unittest.main()
