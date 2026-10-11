
import unittest, importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location(
    "m",ROOT/"scripts"/"e1_historical_price_backfill_v01.py"
)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_quality_good(self):
        df=pd.DataFrame({
            "symbol":["A","A"],
            "trade_date":["2025-01-01","2025-01-02"],
            "open":[10,11],"high":[11,12],"low":[9,10],
            "close":[10.5,11.5],"pre_close":[9.8,10.5]
        })
        q=m.data_quality(m.normalize_bars(df))
        self.assertEqual(q["duplicate_symbol_date_rows"],0)
        self.assertEqual(q["positive_price_rate"],1.0)
        self.assertEqual(q["high_low_consistency_rate"],1.0)

    def test_union_primary_wins_overlap(self):
        a=pd.DataFrame({
            "symbol":["A"],"trade_date":["2025-01-01"],
            "open":[10],"high":[11],"low":[9],"close":[10.5],"pre_close":[9.8]
        })
        b=pd.DataFrame({
            "symbol":["A"],"trade_date":["2025-01-01"],
            "open":[20],"high":[21],"low":[19],"close":[20.5],"pre_close":[19.8]
        })
        z=m.union_shadow(a,b)
        self.assertEqual(len(z),1)
        self.assertEqual(float(z.iloc[0]["open"]),10.0)

    def test_mapping_coverage(self):
        ev=pd.DataFrame({
            "symbol":["A"],
            "event_time":["2025-01-01"],
            "strategy_eligible_semantics":[True],
            "event_kind":["ORIGINAL_FORECAST"],
            "change_type":["TURNAROUND"]
        })
        bars=pd.DataFrame({
            "symbol":["A"],
            "trade_date":["2025-01-02"],
            "open":[10],"high":[11],"low":[9],"close":[10.5],"pre_close":[9.8]
        })
        r=m.entry_mapping_coverage(ev,bars)
        self.assertEqual(r["next_bar_possible_rate"],1.0)

if __name__=="__main__":
    unittest.main()
