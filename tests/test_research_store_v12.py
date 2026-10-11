import tempfile
import unittest
from pathlib import Path
import pandas as pd

from research.research_store import ResearchStore

class ResearchStoreV12Tests(unittest.TestCase):
    def test_register_asset_conflict_update_uses_duckdb_function(self):
        with tempfile.TemporaryDirectory() as td:
            with ResearchStore(Path(td)/"r.duckdb") as store:
                store.register_asset("A","X","x","a","T","READY","20260924",1,{})
                store.register_asset("A","Y","y","b","T","READY","20260925",2,{})
                self.assertEqual(store.con.execute("select asset_kind,row_count from asset_registry where asset_id='A'").fetchone(), ("Y",2))

    def test_external_csv_view_is_not_physical_copy(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            csv=root/"x.csv"
            pd.DataFrame({"a":[1,2,3]}).to_csv(csv,index=False)
            with ResearchStore(root/"r.duckdb") as store:
                r=store.register_external_view("A","foundation_x",csv,"TEST","csv","20260924")
                self.assertEqual(r["status"],"REGISTERED_EXTERNAL_VIEW")
                self.assertEqual(store.con.execute("select count(*) from foundation_x").fetchone()[0],3)
                tables={x[0] for x in store.con.execute("select table_name from duckdb_tables()").fetchall()}
                self.assertNotIn("foundation_x",tables)

if __name__ == "__main__":
    unittest.main()
