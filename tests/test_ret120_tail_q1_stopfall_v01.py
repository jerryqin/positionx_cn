
import unittest,importlib.util
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
sp=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"run_ret120_tail_q1_stopfall_v01.py")
m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m)

class T(unittest.TestCase):
    def test_stopfall_definition(self):
        rows=[]
        closes=list(range(30,10,-1))+[10,11]
        for i,c in enumerate(closes):
            rows.append({"trade_date":pd.Timestamp("2024-01-01")+pd.Timedelta(days=i),
                         "symbol":"000001.SZ","adj_open":float(c),"adj_close":float(c)})
        f=pd.DataFrame(rows).sort_values(["symbol","trade_date"])
        g=f.groupby("symbol")["adj_close"]
        f["prev_close"]=g.shift(1)
        f["prior20_close_min"]=g.transform(lambda s:s.shift(1).rolling(20,min_periods=20).min())
        f["prior_day_is_20d_close_low"]=f["prev_close"].notna() & f["prior20_close_min"].notna() & (f["prev_close"]<=f["prior20_close_min"])
        f["current_day_bounces"]=f["adj_close"]>f["prev_close"]
        f["stopfall_confirm"]=f["prior_day_is_20d_close_low"] & f["current_day_bounces"]
        self.assertTrue(bool(f.iloc[-1]["stopfall_confirm"]))

    def test_date_equal_baseline(self):
        e=pd.DataFrame({
          "signal_date":[pd.Timestamp("2024-01-02")]*3,
          "raw_return":[.10,.00,-.05],
          "stopfall_confirm":[True,False,False]
        })
        z=m.date_equal_stats(e)
        self.assertAlmostEqual(z.iloc[0]["selected_mean"],.10)
        self.assertAlmostEqual(z.iloc[0]["baseline_q1_mean"],(.10+.00-.05)/3)
        self.assertGreater(z.iloc[0]["lift"],0)

    def test_fast_reject(self):
        c={"fast_reject_gate":{"selected_events_min_each_horizon":200,
                               "selected_signal_dates_min_each_horizon":100,
                               "positive_lift_folds_min_each_horizon":2}}
        s={}
        for h in [20,60]:
            s[f"H{h}"]={"selected_events":300,"selected_signal_dates":150,
                        "date_equal_selected_raw_mean":.03,"date_equal_lift":.01,
                        "positive_lift_folds":3}
        self.assertEqual(m.fast_reject(s,c)["status"],"CANDIDATE")

if __name__=="__main__":
    unittest.main()
