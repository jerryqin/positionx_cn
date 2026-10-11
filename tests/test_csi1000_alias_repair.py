import unittest
import pandas as pd

from research.csi1000_alias_repair import (
    OLD_CODE,
    NEW_CODE,
    canonicalize_code_alias,
    rebuild_history,
)


class CSI1000AliasRepairTests(unittest.TestCase):
    def test_pre_change_duplicate_future_alias_is_dropped(self):
        rows = []
        base = [f"{i:06d}.SZ" for i in range(999)]
        # ensure OLD is one of the valid 1000
        syms = set(base)
        syms.add(OLD_CODE)
        for s in sorted(syms | {NEW_CODE}):
            rows.append({"trade_date":"20220601","symbol":s})
        df = pd.DataFrame(rows)
        out, audit = canonicalize_code_alias(df, expected_size=1000)
        ss = set(out["symbol"])
        self.assertEqual(len(ss), 1000)
        self.assertIn(OLD_CODE, ss)
        self.assertNotIn(NEW_CODE, ss)
        self.assertEqual(audit["repaired_bad_size_dates"], 0)

    def test_pre_change_wrong_alias_only_is_replaced(self):
        syms = {f"{i:06d}.SZ" for i in range(999)}
        syms.add(NEW_CODE)
        df = pd.DataFrame([
            {"trade_date":"20240102","symbol":s} for s in sorted(syms)
        ])
        out, _ = canonicalize_code_alias(df, expected_size=1000)
        ss = set(out["symbol"])
        self.assertIn(OLD_CODE, ss)
        self.assertNotIn(NEW_CODE, ss)

    def test_history_rebuild_has_no_inverted_intervals(self):
        df = pd.DataFrame([
            {"trade_date":"20260102","symbol":"A"},
            {"trade_date":"20260105","symbol":"A"},
            {"trade_date":"20260106","symbol":"B"},
        ])
        h = rebuild_history(df)
        for r in h.itertuples(index=False):
            if r.valid_to_exclusive:
                self.assertLess(r.valid_from, r.valid_to_exclusive)


if __name__ == "__main__":
    unittest.main()
