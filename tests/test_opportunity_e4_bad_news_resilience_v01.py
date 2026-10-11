
import unittest, importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location(
    "m",ROOT/"opportunity"/"e4_bad_news_resilience_v01.py"
)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_negative_setup_only(self):
        cfg={"mechanism":{
            "event_kind":"ORIGINAL_FORECAST",
            "negative_change_types":["DETERIORATING","TURN_TO_LOSS","LOSS_WIDENING"]
        }}
        df=pd.DataFrame({
            "symbol":["000001.SZ","000002.SZ"],
            "event_time":["2025-01-01","2025-01-02"],
            "strategy_eligible_semantics":[True,True],
            "event_kind":["ORIGINAL_FORECAST","ORIGINAL_FORECAST"],
            "change_type":["DETERIORATING","IMPROVING"],
            "report_period":["2024_ANNUAL","2024_ANNUAL"]
        })
        z=m.prepare_events(df,cfg)
        self.assertEqual(len(z),1)

    def test_reaction_signal_and_next_session_entry(self):
        ev=pd.DataFrame({
            "canonical_symbol":["000001.SZ"],
            "event_date":pd.to_datetime(["2025-01-01"]),
        })
        bars=pd.DataFrame({
            "symbol":["000001.SZ"]*3,
            "trade_date":pd.to_datetime(["2025-01-02","2025-01-03","2025-01-06"]),
            "adj_open":[10.0,10.5,10.7],
            "adj_close":[10.4,10.6,10.8],
            "adj_high":[10.5,10.7,10.9],
            "adj_low":[9.9,10.4,10.6],
        })
        z=m.attach_reaction_and_entry(ev,bars)
        self.assertTrue(bool(z.loc[0,"e4_signal"]))
        self.assertEqual(str(z.loc[0,"reaction_date"].date()),"2025-01-02")
        self.assertEqual(str(z.loc[0,"entry_date"].date()),"2025-01-03")

    def test_bad_news_red_day_not_signal(self):
        ev=pd.DataFrame({
            "canonical_symbol":["000001.SZ"],
            "event_date":pd.to_datetime(["2025-01-01"]),
        })
        bars=pd.DataFrame({
            "symbol":["000001.SZ"]*2,
            "trade_date":pd.to_datetime(["2025-01-02","2025-01-03"]),
            "adj_open":[10.0,9.5],
            "adj_close":[9.4,9.6],
            "adj_high":[10.1,9.7],
            "adj_low":[9.3,9.4],
        })
        z=m.attach_reaction_and_entry(ev,bars)
        self.assertFalse(bool(z.loc[0,"e4_signal"]))

    def test_matured_only(self):
        g=pd.DataFrame({"adj_high":[1.30]*10,"adj_low":[0.99]*10})
        self.assertTrue(pd.isna(m.matured_label(g,0,1.0,0.25,60,-0.10)))

if __name__=="__main__":
    unittest.main()
