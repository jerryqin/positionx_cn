
import unittest, importlib.util
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"audit_positionx_local_data_inventory_v011.py")
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_eva_not_native(self):
        p=r"D:\positionx_data\incoming\eva\commercial_catalyst_corpus_v0_1\raw\all_meta.parquet"
        self.assertEqual(m.provenance(p),"EVA_INCOMING")

    def test_events_native(self):
        p=r"D:\positionx_data\events\cninfo_general\announcements.parquet"
        self.assertEqual(m.provenance(p),"POSITIONX_NATIVE")

    def test_forecast_family(self):
        p=r"D:\positionx_data\events\forecast_semantic_corpus_v0_1\x.parquet"
        self.assertEqual(m.path_family(p),"EARNINGS_FORECAST_FOCUSED")

if __name__=="__main__":
    unittest.main()
