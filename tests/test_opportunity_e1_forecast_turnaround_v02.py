
import unittest, importlib.util
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"opportunity"/"e1_forecast_turnaround_v02.py")
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_bse_market_canonicalization(self):
        self.assertEqual(m.canonical_equity_symbol("920106.SH"),"920106.BJ")
        self.assertEqual(m.canonical_equity_symbol("920106.BJ"),"920106.BJ")
        self.assertIsNone(m.canonical_equity_symbol("200706.SZ"))

    def test_report_bucket_q1q3_not_q1(self):
        s=pd.Series(["2025_Q1_Q3","2025_Q1"])
        self.assertEqual(m.report_period_bucket(s).tolist(),["Q1_Q3","Q1"])

    def test_matured_label_requires_full_horizon(self):
        g=pd.DataFrame({"adj_high":[1.0,1.30,1.1],"adj_low":[0.98,0.95,0.9]})
        self.assertTrue(pd.isna(m.matured_label(g,0,1.0,0.25,60,-0.10)))

    def test_matured_positive(self):
        g=pd.DataFrame({"adj_high":[1.0]*60,"adj_low":[0.98]*60})
        g.loc[10,"adj_high"]=1.30
        self.assertEqual(m.matured_label(g,0,1.0,0.25,60,-0.10),1)

    def test_identity_events(self):
        df=pd.DataFrame({
            "event_time":["2025-01-01","2025-01-02"],
            "identity_eligible":[True,False],
            "canonical_symbol":["000001.SZ","000002.SZ"],
            "change_type":["TURNAROUND","TURNAROUND"],
            "report_period":["2024_ANNUAL","2024_ANNUAL"]
        })
        z=m.prepare_identity_events(df)
        self.assertEqual(len(z),1)
        self.assertTrue(bool(z.iloc[0]["e1_signal"]))

if __name__=="__main__":
    unittest.main()
