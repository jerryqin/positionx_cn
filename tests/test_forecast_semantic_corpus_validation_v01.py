import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib.util
spec = importlib.util.spec_from_file_location(
    "validator",
    ROOT / "scripts" / "validate_forecast_semantic_corpus_v01.py",
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class T(unittest.TestCase):
    def test_report_period_does_not_create_event_year(self):
        df = pd.DataFrame({
            "event_time": ["2024-01-10"],
            "report_period": ["2018_ANNUAL"],
        })
        t = m.parse_time(df["event_time"])
        self.assertEqual(int(t.dt.year.iloc[0]), 2024)

    def test_semantic_payload_detects_numeric(self):
        df = pd.DataFrame({
            "reported_net_profit_yoy_min_pct": [10.0, None],
            "profit_state": [None, "LOSS"],
        })
        mask = m.semantic_payload_mask(df)
        self.assertEqual(mask.tolist(), [True, True])

    def test_readiness_blocks_short_history(self):
        profile = {
            "status": "SCHEMA_OK",
            "rows": 12090,
            "distinct_symbols": 3800,
            "event_time_nonnull_rate": 1.0,
            "symbol_nonempty_rate": 1.0,
            "announcement_id_nonempty_rate": 1.0,
            "duplicate_announcement_symbol_rows": 0,
            "semantic_payload_rate": 1.0,
            "pdf_sha256_nonempty_rate": 1.0,
            "distinct_event_years": [2024, 2025, 2026],
        }
        cfg = {"gate": {
            "rows_min": 5000,
            "symbols_min": 1000,
            "event_time_nonnull_min": 0.99,
            "symbol_nonempty_min": 0.99,
            "announcement_id_nonempty_min": 0.99,
            "duplicate_identity_max": 0,
            "semantic_payload_min": 0.90,
            "pdf_sha256_min": 0.90,
            "formal_history_years_min": 5,
        }}
        r = m.readiness(profile, cfg)
        self.assertTrue(r["data_quality_pass"])
        self.assertFalse(r["formal_research_ready"])
        self.assertEqual(
            r["verdict"],
            "ACCEPT_CORPUS_QUALITY_BUT_BLOCK_FORMAL_RESEARCH_HISTORY_TOO_SHORT",
        )

    def test_readiness_accepts_five_years(self):
        profile = {
            "status": "SCHEMA_OK",
            "rows": 12090,
            "distinct_symbols": 3800,
            "event_time_nonnull_rate": 1.0,
            "symbol_nonempty_rate": 1.0,
            "announcement_id_nonempty_rate": 1.0,
            "duplicate_announcement_symbol_rows": 0,
            "semantic_payload_rate": 1.0,
            "pdf_sha256_nonempty_rate": 1.0,
            "distinct_event_years": [2022, 2023, 2024, 2025, 2026],
        }
        cfg = {"gate": {
            "rows_min": 5000,
            "symbols_min": 1000,
            "event_time_nonnull_min": 0.99,
            "symbol_nonempty_min": 0.99,
            "announcement_id_nonempty_min": 0.99,
            "duplicate_identity_max": 0,
            "semantic_payload_min": 0.90,
            "pdf_sha256_min": 0.90,
            "formal_history_years_min": 5,
        }}
        r = m.readiness(profile, cfg)
        self.assertTrue(r["formal_research_ready"])

    def test_compare_different_sha_not_interchangeable(self):
        a = {
            "status": "SCHEMA_OK", "sha256": "a", "rows": 10,
            "event_time_min": "2024", "event_time_max": "2026", "distinct_symbols": 5,
        }
        b = dict(a)
        b["sha256"] = "b"
        r = m.compare_profiles([a, b])
        self.assertFalse(r["same_sha256"])
        self.assertTrue(r["same_row_count"])


if __name__ == "__main__":
    unittest.main()
