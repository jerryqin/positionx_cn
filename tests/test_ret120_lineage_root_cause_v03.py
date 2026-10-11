
import unittest, importlib.util
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"diagnose_ret120_lineage_root_cause_v03.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_assign_q5_ties_symbol_asc(self):
        z=pd.DataFrame({"signal_date":["2024-01-02"]*10,
                        "symbol":[f"{i:06d}.SZ" for i in range(10)],
                        "x":[1.0]*10})
        q=m.assign_q5(z,"x")
        self.assertEqual(q[q["bucket"]=="Q5"]["symbol"].tolist(),["000008.SZ","000009.SZ"])

    def test_source_context(self):
        import tempfile
        p=Path(tempfile.gettempdir())/"px_source_context_test.py"
        p.write_text("a=1\nret120=x/x.shift(120)-1\nb=2\n",encoding="utf-8")
        z=m.source_context(p,["ret120"],1)
        self.assertTrue(z)
        self.assertIn("shift(120)",z[0]["text"])

    def test_diff_missingness(self):
        z=pd.DataFrame({"frozen_ret120":[1.0,None,3.0,None],
                        "qmt_ret120":[1.0,2.0,None,None]})
        d=m.summarize_diff(z)
        self.assertEqual(d["both_nan_rows"],1)
        self.assertEqual(d["frozen_valid_qmt_nan_rows"],1)
        self.assertEqual(d["frozen_nan_qmt_valid_rows"],1)

if __name__=="__main__":
    unittest.main()
