from __future__ import annotations
import importlib.util, sys, unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_optimus_seed_debt_v06_apply.py"
spec = importlib.util.spec_from_file_location("v063", SCRIPT)
m = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = m
spec.loader.exec_module(m)

class NullableTimestampTests(unittest.TestCase):
    def test_none_stays_none(self):
        self.assertIsNone(m.nullable_timestamp_value(None))

    def test_string_nat_becomes_none(self):
        self.assertIsNone(m.nullable_timestamp_value("NaT"))

    def test_pandas_nat_string_semantics_become_none(self):
        try:
            import pandas as pd
        except Exception:
            self.skipTest("pandas unavailable")
        self.assertIsNone(m.nullable_timestamp_value(pd.NaT))

    def test_valid_timestamp_is_preserved(self):
        x = "2026-08-25T00:00:00+08:00"
        self.assertEqual(m.nullable_timestamp_value(x), x)

    def test_hengli_product_page_event_time_is_intentionally_unknown(self):
        self.assertIsNone(m.EXTERNAL_EVENT_TIME["HENGLI_IPA_HUMANOID_APPLICATION"])

if __name__ == "__main__":
    unittest.main()
