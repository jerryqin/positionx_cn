import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from research.deep_oversold_universe import (
    CSI500,
    CSI1000,
    audit_universe_assets,
)


class DeepOversoldUniverseTests(unittest.TestCase):
    def _write_assets(self, root: Path, tag: str, expected: int, bad: bool = False):
        rows = []
        for d in ("20260102", "20260105"):
            n = expected + (1 if bad and d == "20260105" else 0)
            for i in range(n):
                rows.append({"trade_date": d, "symbol": f"{i:06d}.SZ"})
        pd.DataFrame(rows).to_csv(root/f"{tag}_membership_daily.csv", index=False)
        pd.DataFrame([{
            "symbol":"000000.SZ",
            "valid_from":"20260102",
            "valid_to_exclusive":"",
        }]).to_csv(root/f"{tag}_membership_history.csv", index=False)
        meta = {
            "四": {
                tag: {
                    "snapshot_crosscheck": {"checked": 2, "exact": 2}
                }
            }
        }
        verify = {
            "四": {
                tag: {
                    "all_days_expected_size": not bad
                }
            }
        }
        (root/"px_assets_meta_346.json").write_text(json.dumps(meta), encoding="utf-8")
        (root/"px_assets_verification.json").write_text(json.dumps(verify), encoding="utf-8")

    def test_csi500_clean_asset_accepts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_assets(root, "csi500", 500, bad=False)
            a = audit_universe_assets(root, CSI500)
            self.assertTrue(a["accepted"])
            self.assertEqual(a["size_min"], 500)
            self.assertEqual(a["size_max"], 500)

    def test_csi1000_1001_day_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_assets(root, "csi1000", 1000, bad=True)
            a = audit_universe_assets(root, CSI1000)
            self.assertFalse(a["accepted"])
            self.assertEqual(a["bad_size_dates_count"], 1)
            self.assertEqual(a["bad_size_dates_sample"][0]["size"], 1001)


if __name__ == "__main__":
    unittest.main()
