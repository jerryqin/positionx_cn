import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_deep_oversold_trade_lineage_v03 as audit


class DeepOversoldTradeLineageV03Tests(unittest.TestCase):

    def sample(self):
        return pd.DataFrame([
            {
                "symbol": "AAA",
                "signal_date": "20200101",
                "entry_date": "20200102",
                "exit_date": "20200110",
            },
            {
                "symbol": "AAA",
                "signal_date": "20200105",
                "entry_date": "20200106",
                "exit_date": "20200115",
            },
            {
                "symbol": "AAA",
                "signal_date": "20200110",
                "entry_date": "20200113",
                "exit_date": "20200120",
            },
            {
                "symbol": "AAA",
                "signal_date": "20200111",
                "entry_date": "20200114",
                "exit_date": "20200121",
            },
        ])

    def test_exit_then_signal_allows_same_day_exit_signal(self):
        accepted, suppressed = audit.suppress_while_active(
            self.sample(),
            allow_signal_on_exit_date=True,
        )
        self.assertEqual(len(accepted), 2)
        self.assertEqual(len(suppressed), 2)
        self.assertEqual(
            accepted["signal_date"].tolist(),
            ["20200101", "20200110"],
        )

    def test_signal_before_exit_blocks_same_day_exit_signal(self):
        accepted, suppressed = audit.suppress_while_active(
            self.sample(),
            allow_signal_on_exit_date=False,
        )
        self.assertEqual(len(accepted), 2)
        self.assertEqual(
            accepted["signal_date"].tolist(),
            ["20200101", "20200111"],
        )

    def test_target_reference(self):
        self.assertEqual(audit.TARGET_COUNT, 1365)

    def test_duckdb_is_used_for_parquet_runtime(self):
        source = Path(audit.__file__).read_text(encoding="utf-8")
        self.assertIn("read_parquet", source)
        self.assertNotIn("pd.read_parquet(REGIME)", source)


if __name__ == "__main__":
    unittest.main()
