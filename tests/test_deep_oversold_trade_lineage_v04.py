import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_deep_oversold_trade_lineage_v04 as audit


class DeepOversoldTradeLineageV04Tests(unittest.TestCase):

    def test_max_slots(self):
        self.assertEqual(audit.MAX_SLOTS, 30)

    def test_open_entry_close_exit_same_day_exit_still_blocks(self):
        rows = []
        # 30 positions enter first, all exit 20200110.
        for i in range(30):
            rows.append({
                "symbol": f"A{i:02d}",
                "signal_date": "20191231",
                "entry_date": "20200102",
                "exit_date": "20200110",
                "signal_ret20": -0.30,
            })
        # One new candidate enters on the same date old positions exit.
        rows.append({
            "symbol": "NEW",
            "signal_date": "20200109",
            "entry_date": "20200110",
            "exit_date": "20200130",
            "signal_ret20": -0.30,
        })
        df = pd.DataFrame(rows)

        accepted, rejected, _ = audit.simulate_slot_cap(
            df,
            exits_release_before_open=False,
        )
        self.assertEqual(len(accepted), 30)
        self.assertEqual(len(rejected), 1)

    def test_diagnostic_same_day_release_accepts(self):
        rows = []
        for i in range(30):
            rows.append({
                "symbol": f"A{i:02d}",
                "signal_date": "20191231",
                "entry_date": "20200102",
                "exit_date": "20200110",
                "signal_ret20": -0.30,
            })
        rows.append({
            "symbol": "NEW",
            "signal_date": "20200109",
            "entry_date": "20200110",
            "exit_date": "20200130",
            "signal_ret20": -0.30,
        })
        df = pd.DataFrame(rows)

        accepted, rejected, _ = audit.simulate_slot_cap(
            df,
            exits_release_before_open=True,
        )
        self.assertEqual(len(accepted), 31)
        self.assertEqual(len(rejected), 0)


if __name__ == "__main__":
    unittest.main()
