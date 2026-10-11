import unittest
import pandas as pd

from research.csi1000_anomaly_forensics import (
    analyze_block,
    contiguous_bad_blocks,
)


class CSI1000AnomalyForensicsTests(unittest.TestCase):
    def test_two_bad_blocks_detected(self):
        dates = ["20220103","20220104","20220105","20220106","20220107","20220110"]
        sets = {
            "20220103":{"A","B"},
            "20220104":{"A","B","X"},
            "20220105":{"A","B","X"},
            "20220106":{"A","B"},
            "20220107":{"A","B","Y"},
            "20220110":{"A","B"},
        }
        blocks = contiguous_bad_blocks(dates, sets, 2)
        self.assertEqual(blocks, [
            ["20220104","20220105"],
            ["20220107"],
        ])

    def test_stable_plus_one_shape(self):
        dates = ["20220103","20220104","20220105","20220106"]
        sets = {
            "20220103":{"A","B"},
            "20220104":{"A","B","X"},
            "20220105":{"A","B","X"},
            "20220106":{"A","C"},
        }
        r = analyze_block(
            dates, sets, ["20220104","20220105"], expected_size=2
        )
        self.assertTrue(r["bad_set_constant_across_block"])
        self.assertEqual(
            r["shape_classification"],
            "STABLE_PLUS_ONE_BLOCK_BETWEEN_CLEAN_1000_SETS",
        )
        self.assertEqual(r["first_bad_vs_prev"]["added"], ["X"])

    def test_raw_snapshot_reader_avoids_pandas_optional_parquet_engine(self):
        import inspect
        import research.csi1000_anomaly_forensics as m
        src = inspect.getsource(m.raw_snapshot_context)
        self.assertIn("duckdb", src)
        self.assertIn("read_parquet", src)
        self.assertNotIn("pd.read_parquet", src)


if __name__ == "__main__":
    unittest.main()
