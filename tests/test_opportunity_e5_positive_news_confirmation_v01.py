
import unittest, importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location(
    "m",ROOT/"opportunity"/"e5_positive_news_confirmation_v01.py"
)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_positive_setup_only(self):
        cfg={"mechanism":{
            "event_kind":"ORIGINAL_FORECAST",
            "positive_change_types":["IMPROVING","TURNAROUND","LOSS_NARROWING"],
        }}
        df=pd.DataFrame({
            "symbol":["000001.SZ","000002.SZ"],
            "event_time":["2025-01-01","2025-01-02"],
            "strategy_eligible_semantics":[True,True],
            "event_kind":["ORIGINAL_FORECAST","ORIGINAL_FORECAST"],
            "change_type":["IMPROVING","DETERIORATING"],
            "report_period":["2024_ANNUAL","2024_ANNUAL"],
        })
        z=m.prepare_events(df,cfg)
        self.assertEqual(len(z),1)

    def test_positive_full_day_confirmation(self):
        ev=pd.DataFrame({
            "canonical_symbol":["000001.SZ"],
            "event_date":pd.to_datetime(["2025-01-01"]),
        })
        bars=pd.DataFrame({
            "symbol":["000001.SZ"]*2,
            "trade_date":pd.to_datetime(["2025-01-02","2025-01-03"]),
            "close":[10.2,10.3],
            "pre_close":[10.0,10.2],
            "adj_open":[10.1,10.25],
            "adj_close":[10.2,10.3],
            "adj_high":[10.3,10.4],
            "adj_low":[10.0,10.2],
        })
        z=m.attach_reaction_and_entry(ev,bars)
        self.assertTrue(bool(z.loc[0,"e5_signal"]))
        self.assertEqual(str(z.loc[0,"entry_date"].date()),"2025-01-03")

    def test_negative_full_day_not_confirmation(self):
        ev=pd.DataFrame({
            "canonical_symbol":["000001.SZ"],
            "event_date":pd.to_datetime(["2025-01-01"]),
        })
        bars=pd.DataFrame({
            "symbol":["000001.SZ"]*2,
            "trade_date":pd.to_datetime(["2025-01-02","2025-01-03"]),
            "close":[9.8,9.9],
            "pre_close":[10.0,9.8],
            "adj_open":[10.1,9.85],
            "adj_close":[9.8,9.9],
            "adj_high":[10.2,10.0],
            "adj_low":[9.7,9.8],
        })
        z=m.attach_reaction_and_entry(ev,bars)
        self.assertFalse(bool(z.loc[0,"e5_signal"]))

    def test_matured_only(self):
        g=pd.DataFrame({"adj_high":[1.30]*10,"adj_low":[0.99]*10})
        self.assertTrue(pd.isna(m.matured_label(g,0,1.0,0.25,60,-0.10)))

if __name__=="__main__":
    unittest.main()
