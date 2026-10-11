from __future__ import annotations
import importlib.util, sys, unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_optimus_source_recall_v046.py"
spec = importlib.util.spec_from_file_location("v046", SCRIPT)
m = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = m
spec.loader.exec_module(m)

class Tests(unittest.TestCase):
    def test_symbol_normalization(self):
        self.assertEqual(m.normalize_symbol("601689"), "601689.SH")
        self.assertEqual(m.normalize_symbol("603119"), "603119.SH")
        self.assertEqual(m.normalize_symbol("300580"), "300580.SZ")

    def test_body_stock_scoped(self):
        q = m.Query("2026-08-28", stock="601689,9900023677")
        s = m.cninfo_body(q, 1).decode()
        self.assertIn("pageNum=1", s)
        self.assertIn("pageSize=30", s)
        self.assertIn("stock=601689%2C9900023677", s)
        self.assertIn("seDate=2026-08-28", s)

    def test_exact_rows(self):
        rows = [
            {"symbol":"601689.SH","announcement_id":"a"},
            {"symbol":"601100.SH","announcement_id":"b"},
        ]
        self.assertEqual(len(m.exact_rows(rows, "601689.SH")), 1)

    def test_targeted_cases(self):
        by_sym = {x["symbol"]: x for x in m.TARGETED_CASES}
        self.assertIn("1225521853", by_sym["601689.SH"]["required_ids"])
        self.assertIn("1225499629", by_sym["601100.SH"]["required_ids"])

    def test_candidate_file(self):
        p = Path(__file__).resolve().parents[1] / "config/research/optimus_source_recall_v046_seed_debt.csv"
        rows = m.read_candidates(p)
        self.assertEqual(len(rows), 5)
        self.assertEqual({x.symbol for x in rows},
                         {"601100.SH","603009.SH","603119.SH","601689.SH","600699.SH"})

if __name__ == "__main__":
    unittest.main()
