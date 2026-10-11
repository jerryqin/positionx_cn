import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import daily_pit_intake_v01 as pit


class DailyPITV01Tests(unittest.TestCase):

    def test_index_counts_frozen(self):
        self.assertEqual(pit.INDEX_SECTORS["000300.SH"][1], 300)
        self.assertEqual(pit.INDEX_SECTORS["000905.SH"][1], 500)
        self.assertEqual(pit.INDEX_SECTORS["000852.SH"][1], 1000)

    def test_financial_revisit_window_not_seven_days(self):
        for name in ("forecast", "express", "income", "fina_indicator"):
            self.assertGreaterEqual(
                pit.CLOUD_DATASETS[name]["revisit_days"],
                180,
            )

    def test_payload_hash_stable(self):
        a = {"x": 1, "y": "abc"}
        b = {"y": "abc", "x": 1}
        self.assertEqual(
            pit.payload_hash_row(a),
            pit.payload_hash_row(b),
        )

    def test_stock_limit_old_source_is_stale(self):
        old = (pit.now_cst().date() - pit.timedelta(days=20)).strftime(
            "%Y-%m-%d"
        )
        self.assertEqual(
            pit.cloud_health("stock_limit", old),
            "STALE",
        )

    def test_event_tables_not_declared_fresh_from_max_date(self):
        today = pit.now_cst().strftime("%Y-%m-%d")
        for name in ("forecast", "express", "income", "fina_indicator"):
            self.assertEqual(
                pit.cloud_health(name, today),
                "UNKNOWN",
            )


if __name__ == "__main__":
    unittest.main()
