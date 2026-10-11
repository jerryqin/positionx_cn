
import sys, unittest
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

from opportunity.d5_leader_laggard_propagation import add_leader_propagation_features, evaluate_d5

class T(unittest.TestCase):
    def make_panel(self, periods=180, members=20):
        dates=pd.date_range("2024-01-01",periods=periods,freq="D").strftime("%Y%m%d")
        rows=[]
        for di,d in enumerate(dates):
            for si in range(members):
                boost=(di%17)/100.0 if si>=16 else 0.0
                rows.append({
                    "trade_date":d,"symbol":f"S{si:02d}",
                    "sector_key":"SW2021|L1|10",
                    "ret20":si/100.0+boost,
                })
        return pd.DataFrame(rows)

    def test_signal_subset(self):
        z=add_leader_propagation_features(self.make_panel(),history_sessions=20)
        self.assertTrue((~z["d5_signal"] | z["follower_setup"]).all())

    def test_threshold_exists(self):
        z=add_leader_propagation_features(self.make_panel(),history_sessions=20)
        self.assertGreater(z["leader_impulse_prior_q75"].notna().sum(),0)

    def test_leader_spread_positive(self):
        z=add_leader_propagation_features(self.make_panel(30),history_sessions=5)
        self.assertTrue((z["leader_spread20"].dropna()>0).all())

    def test_follower_band(self):
        z=add_leader_propagation_features(self.make_panel(5),history_sessions=2)
        f=z[z["follower_setup"]]
        self.assertTrue((f["sector_ret20_rank_d5"]>=0.40).all())
        self.assertTrue((f["sector_ret20_rank_d5"]<0.70).all())

    def test_baseline(self):
        rows=[]
        for y in range(2020,2026):
            for i in range(20):
                rows.append({
                    "trade_date":f"{y}01{i+1:02d}",
                    "follower_setup":True,
                    "d5_signal":i<5,
                    "D4_TREND25_60_label":1 if i<4 else 0,
                })
        r=evaluate_d5(pd.DataFrame(rows),"D4_TREND25_60_label",1,1.0,0.0,1)
        self.assertEqual(r["setup_count"],120)
        self.assertEqual(r["signal_count"],30)
        self.assertGreater(r["signal_rate"],r["setup_rate"])

if __name__=="__main__":
    unittest.main()
