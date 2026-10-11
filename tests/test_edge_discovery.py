import unittest
import numpy as np
import pandas as pd
from research.edge_discovery import expand_membership_intervals, symbol_frame, market_state

class EdgeDiscoveryTests(unittest.TestCase):
    def test_interval_end_exclusive(self):
        x=pd.DataFrame([{"symbol":"A","valid_from":"20260102","valid_to_exclusive":"20260105"}])
        y=expand_membership_intervals(x,["20260101","20260102","20260103","20260105"])
        self.assertEqual(y["date"].tolist(),["20260102","20260103"])

    def test_exact_market_exit_missing_stays_nan(self):
        dates=[f"2026010{i}" for i in range(1,9)]
        df=pd.DataFrame({
            "open":[10,11,12,13,14,15,16,17],
            "high":[11,12,13,14,15,16,17,18],
            "low":[9,10,11,12,13,14,15,16],
            "close":[10,11,12,13,14,15,np.nan,17],
            "volume":[100]*8,"amount":[1e8]*8,
        },index=dates)
        f=symbol_frame("A",df,dates)
        # Signal on day2 enters day3 open; h=5 exit is exactly day7 close.
        # Day7 is missing, so the return must remain NaN, not slide to day8.
        row=f.loc[f["date"]==dates[1]].iloc[0]
        self.assertTrue(pd.isna(row["fwd_5"]))

    def test_market_state_up_broad(self):
        x=pd.DataFrame({
            "candidate":[True]*100,
            "above_ma20":[True]*70+[False]*30,
            "above_ma60":[True]*60+[False]*40,
            "ret_20":np.linspace(-.1,.2,100),
            "benchmark_close":[110]*100,"benchmark_ma60":[100]*100,
        })
        self.assertEqual(market_state(x)["state"],"UP_BROAD")

if __name__=="__main__":
    unittest.main()
