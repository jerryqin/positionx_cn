"""Exact-day pool reads on disposable databases; no provider or production access."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from core import p0_rq_input as rq


class CurrentIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'daily.duckdb'
        with duckdb.connect(str(self.db), config={'temp_directory': ''}) as con:
            con.execute("""CREATE TABLE identity_observed_today AS
                SELECT lpad(CAST(i AS VARCHAR),6,'0') || '.XSHE' AS order_book_id
                FROM range(1,3001) r(i)""")
            con.execute('CREATE TABLE security_identity AS SELECT * FROM identity_observed_today')
            con.execute("INSERT INTO security_identity VALUES ('999999.XSHE')")
        self.input = SimpleNamespace(db=self.db, meta={'asof': '2026-10-27',
                                                     'business_date': '2026-10-27'})

    def full_market(self):
        with patch.object(rq, 'open_inputs', return_value=self.input):
            return rq.full_market('20261027')

    def test_current_pool_excludes_retained_absence_without_deleting_history(self):
        before = rq.sha(self.db)
        symbols, errors = self.full_market()
        self.assertEqual(len(symbols), 3000)
        self.assertNotIn('999999.SZ', symbols)
        self.assertEqual(errors, [])
        self.assertEqual(rq.sha(self.db), before)

    def test_wrong_business_date_is_rejected(self):
        self.input.meta['business_date'] = '2026-10-26'
        with self.assertRaisesRegex(rq.InputBlocked, 'EXACT_DAY_IDENTITY_BINDING_MISSING'):
            self.full_market()

    def test_missing_current_pool_does_not_fall_back_to_inventory(self):
        with duckdb.connect(str(self.db)) as con:
            con.execute('DROP TABLE identity_observed_today')
        with self.assertRaisesRegex(rq.InputBlocked, 'EXACT_DAY_IDENTITY_MISSING'):
            self.full_market()

    def test_duplicate_current_identity_is_rejected(self):
        with duckdb.connect(str(self.db)) as con:
            con.execute('INSERT INTO identity_observed_today SELECT * FROM identity_observed_today LIMIT 1')
        with self.assertRaisesRegex(rq.InputBlocked, 'DUPLICATE_EXACT_DAY_IDENTITY'):
            self.full_market()


if __name__ == '__main__':
    unittest.main()
