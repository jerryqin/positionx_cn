
import unittest, importlib.util
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"opportunity"/"e2_positive_forecast_revision_v01.py")
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_equity_domain(self):
        self.assertEqual(m.canonical_equity_symbol("920106.SH"),"920106.BJ")
        self.assertEqual(m.canonical_equity_symbol("000001.SZ"),"000001.SZ")
        self.assertIsNone(m.canonical_equity_symbol("200706.SZ"))
    def test_first_revision_only(self):
        cfg={"mechanism":{"positive_change_types":["IMPROVING"],"setup_event_kinds":["ORIGINAL_FORECAST","FORECAST_REVISION"],"signal_event_kind":"FORECAST_REVISION"}}
        df=pd.DataFrame({
            "announcementId":["a","b","c"],
            "symbol":["000001.SZ"]*3,
            "event_time":["2025-01-01","2025-01-02","2025-01-03"],
            "strategy_eligible_semantics":[True]*3,
            "change_type":["IMPROVING"]*3,
            "event_kind":["ORIGINAL_FORECAST","FORECAST_REVISION","FORECAST_REVISION"],
            "report_period":["2024_ANNUAL"]*3
        })
        z=m.prepare_events(df,cfg)
        self.assertEqual(int(z["e2_signal"].sum()),1)
    def test_matured_only(self):
        g=pd.DataFrame({"adj_high":[1.3]*10,"adj_low":[0.99]*10})
        self.assertTrue(pd.isna(m.matured_label(g,0,1.0,0.25,60,-0.10)))

if __name__=="__main__":
    unittest.main()
