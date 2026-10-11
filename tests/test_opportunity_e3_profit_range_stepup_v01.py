
import unittest, importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"opportunity"/"e3_profit_range_stepup_v01.py")
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_threshold_free_stepup(self):
        cfg={"mechanism":{"event_kind":"ORIGINAL_FORECAST","change_type":"IMPROVING"}}
        df=pd.DataFrame({
            "symbol":["000001.SZ","000002.SZ"],
            "event_time":["2025-01-01","2025-01-02"],
            "strategy_eligible_semantics":[True,True],
            "event_kind":["ORIGINAL_FORECAST","ORIGINAL_FORECAST"],
            "change_type":["IMPROVING","IMPROVING"],
            "report_period":["2024_ANNUAL","2024_ANNUAL"],
            "current_profit_min_yuan":[120,100],
            "current_profit_max_yuan":[140,130],
            "prior_profit_min_yuan":[80,80],
            "prior_profit_max_yuan":[110,110],
        })
        z=m.prepare_events(df,cfg)
        self.assertEqual(z["e3_signal"].tolist(),[True,False])

    def test_turnaround_excluded_by_prior_positive(self):
        cfg={"mechanism":{"event_kind":"ORIGINAL_FORECAST","change_type":"IMPROVING"}}
        df=pd.DataFrame({
            "symbol":["000001.SZ"],"event_time":["2025-01-01"],
            "strategy_eligible_semantics":[True],
            "event_kind":["ORIGINAL_FORECAST"],"change_type":["IMPROVING"],
            "report_period":["2024_ANNUAL"],
            "current_profit_min_yuan":[100],"current_profit_max_yuan":[120],
            "prior_profit_min_yuan":[-20],"prior_profit_max_yuan":[-10],
        })
        self.assertEqual(len(m.prepare_events(df,cfg)),0)

    def test_matured_only(self):
        g=pd.DataFrame({"adj_high":[1.30]*10,"adj_low":[0.99]*10})
        self.assertTrue(pd.isna(m.matured_label(g,0,1.0,0.25,60,-0.10)))

if __name__=="__main__":
    unittest.main()
