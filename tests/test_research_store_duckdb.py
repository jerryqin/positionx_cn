import tempfile
import unittest
from pathlib import Path
import pandas as pd
from research.research_store import ResearchStore

class ResearchStoreDuckDBCompatibilityTests(unittest.TestCase):
    def test_register_asset_conflict_update(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / 'r.duckdb'
            with ResearchStore(db) as store:
                store.register_asset('A','TEST','x.csv','abc','TEST','READY','20260924',1,{'v':1})
                store.register_asset('A','TEST2','y.csv','def','TEST','READY','20260925',2,{'v':2})
                row = store.con.execute("SELECT asset_kind, source_sha256, row_count FROM asset_registry WHERE asset_id='A'").fetchone()
                self.assertEqual(row, ('TEST2','def',2))

    def test_save_state_conflict_update(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / 'r.duckdb'
            with ResearchStore(db) as store:
                store.save_state('S','h1',{'x':1})
                store.save_state('S','h1',{'x':2})
                self.assertEqual(store.load_state('S')['x'], 2)

    def test_daily_bar_conflict_update(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / 'r.duckdb'
            with ResearchStore(db) as store:
                bars = pd.DataFrame([{'symbol':'000001.SZ','trade_date':'20260924','open':10.0,'high':10.5,'low':9.8,'close':10.2,'volume':1000,'amount':10000}])
                store.upsert_bars(bars,'front','TEST')
                bars.loc[0,'close'] = 10.3
                store.upsert_bars(bars,'front','TEST2')
                row = store.con.execute("SELECT close, source_system FROM daily_bars WHERE symbol='000001.SZ' AND trade_date='20260924' AND dividend_type='front'").fetchone()
                self.assertAlmostEqual(row[0], 10.3)
                self.assertEqual(row[1], 'TEST2')

if __name__ == '__main__':
    unittest.main()
