import sys, unittest, importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location(
    "v",ROOT/"scripts"/"validate_eva_forecast_parser_v02.py"
)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_strict_payload_rejects_unknown(self):
        df=pd.DataFrame({
            "profit_state":["UNKNOWN","PROFIT"],
            "change_type":[None,None],
        })
        self.assertEqual(m.strict_payload_mask(df).tolist(),[False,True])

    def test_strict_payload_numeric(self):
        df=pd.DataFrame({
            "current_profit_min_yuan":[None,1.0],
            "profit_state":[None,None],
        })
        self.assertEqual(m.strict_payload_mask(df).tolist(),[False,True])

    def test_norm_bool(self):
        self.assertEqual(
            m.norm_bool(pd.Series([True,False,"True","0"])).tolist(),
            [True,False,True,False]
        )

    def test_datetime_rate(self):
        a=pd.Series(["2020-01-01","2020-01-02 10:00:00"])
        b=pd.Series(["2020-01-01 00:00:00","2020-01-02 10:00:00"])
        self.assertEqual(m.datetime_rate(a,b),1.0)

    def test_exact_rate_nulls(self):
        a=pd.Series([None,"A"])
        b=pd.Series([None,"A"])
        self.assertEqual(m.exact_rate(a,b),1.0)

    def test_yearly_stats(self):
        df=pd.DataFrame({
            "event_time":["2020-01-01","2020-02-01"],
            "strategy_eligible_semantics":[True,True],
            "current_profit_min_yuan":[1.0,None],
            "current_profit_max_yuan":[2.0,None],
            "profit_state":[None,"PROFIT"],
        })
        r=m.yearly_eligible_stats(df)
        self.assertEqual(r["2020"]["eligible_rows"],2)
        self.assertEqual(r["2020"]["eligible_strict_payload_rate"],1.0)
        self.assertEqual(r["2020"]["eligible_current_profit_min_coverage"],0.5)

if __name__=="__main__":
    unittest.main()
