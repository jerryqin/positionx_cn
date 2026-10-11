
import sys, unittest
from pathlib import Path
import numpy as np
import pandas as pd
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from opportunity.d3_market_breadth_ignition import synthetic_adjust_close, add_forward_labels_from_t1, add_breadth_and_signal, evaluate_d3

class T(unittest.TestCase):
    def bars(self, n=180):
        close=np.linspace(10.0,15.0,n)
        return pd.DataFrame({
            "symbol":"A.SH",
            "trade_date":pd.date_range("2025-01-01",periods=n,freq="D").strftime("%Y%m%d"),
            "open":close*0.995,"high":close*1.02,"low":close*0.98,"close":close,
            "pre_close":np.r_[close[0],close[:-1]],
        })
    def test_ret20_and_ma20_exist(self):
        z=synthetic_adjust_close(self.bars())
        self.assertIn("ret20",z.columns); self.assertIn("above_ma20",z.columns)
        self.assertGreater(z["ret20"].notna().sum(),0)
    def test_labels_from_t1_open(self):
        z=add_forward_labels_from_t1(synthetic_adjust_close(self.bars()))
        i=30; entry=float(z.iloc[i+1]["adj_open"]); mx=float(z.iloc[i+1:i+61]["adj_close"].max())
        self.assertAlmostEqual(float(z.iloc[i]["D3_TREND25_60_mfe"]), mx/entry-1.0)
    def test_breadth_history_excludes_current(self):
        dates=pd.date_range("2024-01-01",periods=150,freq="D").strftime("%Y%m%d")
        rows=[]
        for di,d in enumerate(dates):
            for si in range(8):
                rows.append({"trade_date":d,"symbol":f"S{si}","ret20":0.01*si,"above_ma20":si<((di%8)+1)})
        x=add_breadth_and_signal(pd.DataFrame(rows),breadth_history_sessions=20)
        self.assertGreater(x["breadth_delta5_prior_q75"].notna().sum(),0)
    def test_signal_subset_of_leaders(self):
        dates=pd.date_range("2024-01-01",periods=160,freq="D").strftime("%Y%m%d")
        rows=[]
        for di,d in enumerate(dates):
            for si in range(12):
                rows.append({"trade_date":d,"symbol":f"S{si}","ret20":si/100.0,"above_ma20":si<(2+di%10)})
        z=add_breadth_and_signal(pd.DataFrame(rows),breadth_history_sessions=20)
        self.assertTrue((~z["d3_signal"] | z["leader_setup"]).all())
    def test_baseline_is_leaders(self):
        rows=[]
        for y in range(2020,2026):
            for i in range(20):
                rows.append({"trade_date":f"{y}01{i+1:02d}","leader_setup":True,"d3_signal":i<5,"D3_TREND25_60_label":1 if i<4 else 0})
        r=evaluate_d3(pd.DataFrame(rows),"D3_TREND25_60_label",1,1.0,0.0,1)
        self.assertEqual(r["leader_setup_count"],120); self.assertEqual(r["breadth_signal_count"],30)
        self.assertGreater(r["breadth_signal_rate"],r["leader_setup_rate"])
if __name__=="__main__": unittest.main()
