import sqlite3
import unittest
from pathlib import Path
from research.reference_data import csi300_members_at, csi300_is_member

DB = Path(__file__).resolve().parents[1] / "data" / "research" / "positionx_reference.db"


class CSI300ReferenceTests(unittest.TestCase):
    def test_daily_count_is_300(self):
        self.assertEqual(len(csi300_members_at("20210601", DB)), 300)
        self.assertEqual(len(csi300_members_at("20260924", DB)), 300)

    def test_202509_effective_date_is_official_delisting_date(self):
        self.assertTrue(csi300_is_member("601989.SH", "20250904", DB))
        self.assertFalse(csi300_is_member("601298.SH", "20250904", DB))
        self.assertFalse(csi300_is_member("601989.SH", "20250905", DB))
        self.assertTrue(csi300_is_member("601298.SH", "20250905", DB))

    def test_reentry_is_not_collapsed(self):
        with sqlite3.connect(DB) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM csi300_membership_interval WHERE symbol='601298.SH'"
            ).fetchone()[0]
        self.assertEqual(n, 2)

    def test_interval_semantics_match_daily(self):
        for d in ("20201211", "20201214", "20250304", "20250905", "20260615"):
            daily = set(csi300_members_at(d, DB))
            with sqlite3.connect(DB) as conn:
                rows = conn.execute(
                    """SELECT symbol FROM csi300_membership_interval
                       WHERE valid_from<=?
                         AND (valid_to_exclusive IS NULL OR valid_to_exclusive>?)""",
                    (d, d),
                ).fetchall()
            self.assertEqual(daily, {r[0] for r in rows})

    def test_registry_is_ready(self):
        with sqlite3.connect(DB) as conn:
            row = conn.execute(
                "SELECT dataset_id,status FROM dataset_registry"
            ).fetchone()
        self.assertEqual(row[0], "CSI300_PIT_CANONICAL_V1_20260925")
        self.assertEqual(row[1], "READY_FOR_RESEARCH")


if __name__ == "__main__":
    unittest.main()
