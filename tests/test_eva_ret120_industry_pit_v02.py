import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_eva_ret120_industry_pit_v02 as v


class EvaIndustryValidationV02Tests(unittest.TestCase):

    def base_rows(self):
        return pd.DataFrame([
            {
                "symbol": "000001.SZ",
                "effective_from": "20200101",
                "effective_to_exclusive": "20210730",
                "industry_code": "48",
                "industry_name": "银行",
                "standard": "SW2014",
                "level": "L1",
                "source_id": "src",
                "source_url": "url",
                "provenance_note": "note",
            },
            {
                "symbol": "000001.SZ",
                "effective_from": "20210730",
                "effective_to_exclusive": None,
                "industry_code": "48",
                "industry_name": "银行",
                "standard": "SW2021",
                "level": "L1",
                "source_id": "src",
                "source_url": "url",
                "provenance_note": "note",
            },
        ])

    def test_two_generations_are_valid_same_family(self):
        df = self.base_rows()
        self.assertEqual(v.validate_taxonomy_eras(df), [])

    def test_sw2014_cannot_cross_boundary(self):
        df = self.base_rows()
        df.loc[0, "effective_to_exclusive"] = "20210801"
        issues = v.validate_taxonomy_eras(df)
        self.assertTrue(any(x[1] == "SW2014_CROSSES_BOUNDARY" for x in issues))

    def test_sw2021_cannot_start_before_boundary(self):
        df = self.base_rows()
        df.loc[1, "effective_from"] = "20210729"
        issues = v.validate_taxonomy_eras(df)
        self.assertTrue(any(x[1] == "SW2021_START_BEFORE_BOUNDARY" for x in issues))

    def test_sw2014_cannot_be_open_ended(self):
        df = self.base_rows()
        df.loc[0, "effective_to_exclusive"] = None
        issues = v.validate_taxonomy_eras(df)
        self.assertTrue(any(x[1] == "SW2014_OPEN_ENDED" for x in issues))

    def test_overlap_detected(self):
        df = self.base_rows()
        df.loc[0, "effective_to_exclusive"] = "20210801"
        issues = v.validate_intervals(df)
        self.assertTrue(any(x[1] == "OVERLAPPING_INTERVAL" for x in issues))

    def test_unexpected_standard_rejected(self):
        df = self.base_rows()
        df.loc[1, "standard"] = "GICS"
        issues = v.validate_taxonomy_eras(df)
        self.assertTrue(any(x[1] == "UNEXPECTED_STANDARD" for x in issues))


if __name__ == "__main__":
    unittest.main()
