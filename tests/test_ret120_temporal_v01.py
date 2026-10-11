import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_ret120_temporal_v01 as audit


class Ret120TemporalV01Tests(unittest.TestCase):

    def test_normalize_date_accepts_real_date(self):
        self.assertEqual(audit.normalize_date("2026-09-24"), "20260924")
        self.assertEqual(audit.normalize_date("20260924"), "20260924")

    def test_normalize_date_rejects_impossible_calendar_date(self):
        self.assertIsNone(audit.normalize_date("20235691"))
        self.assertIsNone(audit.normalize_date("20261301"))
        self.assertIsNone(audit.normalize_date("20260230"))

    def test_extract_list_schema(self):
        obj = {
            "folds": [
                {"name": "F1", "start": "2020-01-01", "end": "2021-01-01"},
                {"name": "F2", "start": "2021-01-02", "end": "2022-01-01"},
                {"name": "F3", "start": "2022-01-02", "end": "2023-01-01"},
            ]
        }
        got = audit.extract_named_folds(obj)
        self.assertEqual(got["F1"], ("20200101", "20210101"))
        self.assertEqual(got["F3"], ("20220102", "20230101"))

    def test_extract_direct_schema(self):
        obj = {
            "F1": {"start_date": "20200101", "end_date": "20210101"},
            "F2": {"start_date": "20210102", "end_date": "20220101"},
            "F3": {"start_date": "20220102", "end_date": "20230101"},
        }
        got = audit.extract_named_folds(obj)
        self.assertEqual(set(got), {"F1", "F2", "F3"})

    def test_extract_extended_json_keys(self):
        obj = {
            "fold_results": [
                {"fold": "F1", "first_date": "20200101", "last_date": "20210101"},
                {"fold": "F2", "first_date": "20210102", "last_date": "20220101"},
                {"fold": "F3", "first_date": "20220102", "last_date": "20230101"},
            ]
        }
        got = audit.extract_named_folds(obj)
        self.assertEqual(got["F2"], ("20210102", "20220101"))

    def test_invalid_numeric_noise_is_ignored(self):
        text = """
        F1: 20200803 -> 20221017
        F2: 20221018 -> 20240919
        F3: 20240920 -> 20260924
        noise = "F1 20235691 20951028"
        """
        got = audit.extract_named_folds_from_text(text)
        self.assertEqual(got["F1"], ("20200803", "20221017"))

    def test_assign_fold(self):
        folds = {
            "F1": ("20200101", "20210101"),
            "F2": ("20210102", "20220101"),
            "F3": ("20220102", "20230101"),
        }
        self.assertEqual(audit.assign_fold("2021-06-01", folds), "F2")
        self.assertIsNone(audit.assign_fold("2019-12-31", folds))

    def test_trend_scope_accepts_trend_edge(self):
        p = Path(r"D:\positionx_min_live\scripts\run_edge_discovery_trend_replication_v022.py")
        self.assertTrue(audit._is_trend_provenance_file(p))

    def test_trend_scope_rejects_deep_oversold(self):
        p = Path(r"D:\positionx_min_live\scripts\run_deep_oversold_csi500_universe.py")
        self.assertFalse(audit._is_trend_provenance_file(p))

    def test_trend_scope_rejects_shared_equity(self):
        p = Path(r"D:\positionx_min_live\scripts\run_shared_equity_robustness_audit.py")
        self.assertFalse(audit._is_trend_provenance_file(p))


if __name__ == "__main__":
    unittest.main()
