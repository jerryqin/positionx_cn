import importlib.util, sys, types, unittest
from pathlib import Path
import pandas as pd

if "duckdb" not in sys.modules:
    d=types.ModuleType("duckdb"); d.DuckDBPyConnection=object
    d.connect=lambda *a,**k: (_ for _ in ()).throw(RuntimeError("duckdb stub")); sys.modules["duckdb"]=d
ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts"/"run_optimus_m_evidence_v03.py"
spec=importlib.util.spec_from_file_location("m",SCRIPT); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class MEvidenceV03Tests(unittest.TestCase):
    def test_digest_stable(self):
        df=pd.DataFrame([
            {"symbol":"002050.SZ","company":"A","component_domain":"x","lifecycle_state":"ACTIVE_SEED","f_status":"POSSIBLE","canonical":False,"state_basis":"SEED"},
            {"symbol":"601689.SH","company":"B","component_domain":"y","lifecycle_state":"ACTIVE_CANONICAL","f_status":"PROBABLE","canonical":True,"state_basis":"RULE"},
        ])
        self.assertEqual(m.candidate_registry_digest(df),m.candidate_registry_digest(df.iloc[::-1].reset_index(drop=True)))

    def test_m_state_weak(self):
        r={"data_status":"OK","rs20_vs_broad":-0.1,"rs60_vs_broad":0.2,"above_ma20":False,"above_ma60":True,"above_ma120":True,"amount20_vs_60":1.5,"within_3pct_60d_high":True}
        self.assertEqual(m.m_state(r,1.10),"M_WEAK")

    def test_m_state_improving(self):
        r={"data_status":"OK","rs20_vs_broad":0.1,"rs60_vs_broad":-0.1,"above_ma20":True,"above_ma60":False,"above_ma120":False,"amount20_vs_60":1.1,"within_3pct_60d_high":False}
        self.assertEqual(m.m_state(r,1.10),"M_IMPROVING")

if __name__=="__main__": unittest.main()
