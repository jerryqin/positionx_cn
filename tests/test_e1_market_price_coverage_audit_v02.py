
import unittest, importlib.util
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("a",ROOT/"scripts"/"audit_e1_market_price_coverage_v02.py")
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
class T(unittest.TestCase):
    def test_board(self):
        self.assertEqual(m.board("600000.SH"),"SH_MAIN")
        self.assertEqual(m.board("300001.SZ"),"CHINEXT")
        self.assertEqual(m.board("200706.SZ"),"SZ_B")
    def test_setup(self):
        df=pd.DataFrame({"announcementId":["1","2"],"symbol":["000001.SZ","000002.SZ"],
        "event_time":["2025-01-01","2025-01-02"],"strategy_eligible_semantics":[True,True],
        "event_kind":["ORIGINAL_FORECAST","FOLLOWUP_DOCUMENT"],"change_type":["TURNAROUND","TURNAROUND"]})
        z=m.setup_events(df); self.assertEqual(len(z),1); self.assertEqual(z.iloc[0]["symbol_key"],"000001.SZ")
    def test_table_stats(self):
        setup=pd.DataFrame({"symbol_key":["A","B"],"event_date":[pd.Timestamp("2025-01-01")]*2,"event_year":[2025,2025]})
        prof=pd.DataFrame({"symbol_key":["A"],"min_trade_date":[pd.Timestamp("2020-01-01")],
        "max_trade_date":[pd.Timestamp("2026-01-01")],"rows":[100]})
        z,s=m.table_stats(setup,prof,"x"); self.assertEqual(z["possible"].tolist(),[True,False]); self.assertEqual(s["next_bar_possible_rate"],0.5)
if __name__=="__main__": unittest.main()
