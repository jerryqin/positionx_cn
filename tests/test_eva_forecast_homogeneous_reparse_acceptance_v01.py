import sys, unittest, importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location(
    "v",ROOT/"scripts"/"validate_eva_forecast_homogeneous_reparse_v01.py"
)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_normalize_text(self):
        s=pd.Series([" A 股\n公告 ","Ａ股公告"])
        z=m.normalize_text(s)
        self.assertEqual(z.iloc[0],"A股公告")
        self.assertEqual(z.iloc[1],"A股公告")

    def test_datetime_match(self):
        a=pd.Series(["2024-01-01 00:00:00","2024-01-02"])
        b=pd.Series(["2024-01-01","2024-01-02 00:00:00"])
        self.assertEqual(m.datetime_match_rate(a,b),1.0)

    def test_numeric_agreement(self):
        r=m.numeric_agreement(pd.Series([1.0,2.0,None]),pd.Series([1.0,3.0,4.0]))
        self.assertEqual(r["both_nonnull_rows"],2)
        self.assertAlmostEqual(r["exact_rate"],0.5)

    def test_categorical_agreement(self):
        r=m.categorical_agreement(pd.Series(["A","B",None]),pd.Series(["A","C","D"]))
        self.assertEqual(r["both_nonnull_rows"],2)
        self.assertAlmostEqual(r["match_rate"],0.5)

    def test_semantic_payload_ignores_unknown(self):
        df=pd.DataFrame({
            "profit_state":["UNKNOWN",None],
            "change_type":[None,"TURNAROUND"],
        })
        self.assertAlmostEqual(m.semantic_payload_rate(df),0.5)

    def test_identity_constant(self):
        self.assertEqual(m.IDENTITY,["announcementId","symbol"])

if __name__=="__main__":
    unittest.main()
