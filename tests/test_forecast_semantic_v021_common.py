import sys
import unittest
import importlib.util
from pathlib import Path

import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location(
    "c",ROOT/"scripts"/"_forecast_semantic_v021_common.py"
)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_strict_payload(self):
        df=pd.DataFrame({
            "current_profit_min_yuan":[None,1.0],
            "profit_state":["UNKNOWN",None],
        })
        self.assertEqual(m.strict_payload_mask(df).tolist(),[False,True])

    def test_profile_identity(self):
        df=pd.DataFrame({
            "announcementId":["1","2"],
            "symbol":["A","B"],
            "secCode":["A","B"],"secName":["a","b"],
            "event_time":["2024-01-01","2025-01-01"],
            "event_precision":["DAY","DAY"],"event_date":["2024-01-01","2025-01-01"],
            "title":["x","y"],"event_kind":["ORIGINAL_FORECAST"]*2,
            "explicit_st_at_event":[False,False],
            "strategy_eligible_semantics":[True,True],
            "exclusion_reason":[None,None],
            "report_period":["2023_ANNUAL","2024_ANNUAL"],
            "current_profit_min_yuan":[1.0,2.0],
            "current_profit_max_yuan":[1.0,2.0],
            "prior_profit_min_yuan":[None,None],"prior_profit_max_yuan":[None,None],
            "profit_state":["PROFIT","PROFIT"],
            "reported_net_profit_yoy_min_pct":[None,None],
            "reported_net_profit_yoy_max_pct":[None,None],
            "profit_change_vs_prior_abs_pct":[None,None],
            "change_type":[None,None],"change_basis":[None,None],
            "parser_confidence":["HIGH","HIGH"],"pdf_path":["a","b"],"pdf_sha256":["x","y"],
            "parser_version":["v0.2.1","v0.2.1"],"parser_rule_id":["BASE","BASE"],
        })
        p=m.profile(df)
        self.assertEqual(p["rows"],2)
        self.assertEqual(p["distinct_identity"],2)
        self.assertEqual(p["duplicate_identity_rows"],0)

    def test_gate_parser_version(self):
        p={
            "schema_ok":True,"rows":2,"distinct_identity":2,"duplicate_identity_rows":0,
            "event_time_nonnull_rate":1.0,"event_years":[2024,2025],
            "parser_version_counts":{"v0.2.1":2},
            "eligible_strict_payload_rate":1.0,
            "eligible_current_profit_min_coverage":1.0,
            "eligible_current_profit_max_coverage":1.0,
            "yearly":{
                "2024":{"eligible_strict_payload_rate":1.0,"eligible_current_profit_min_coverage":1.0,"eligible_current_profit_max_coverage":1.0},
                "2025":{"eligible_strict_payload_rate":1.0,"eligible_current_profit_min_coverage":1.0,"eligible_current_profit_max_coverage":1.0},
            }
        }
        gate={
            "identity_rows_exact":2,"event_years_exact":[2024,2025],
            "formal_history_years_min":2,
            "eligible_strict_payload_min":0.99,
            "eligible_yearly_strict_payload_min":0.99,
            "eligible_current_profit_min_coverage_min":0.95,
            "eligible_current_profit_max_coverage_min":0.95,
            "eligible_yearly_current_profit_min_coverage_min":0.95,
            "eligible_yearly_current_profit_max_coverage_min":0.95,
        }
        r=m.evaluate_research_gate(p,gate,"v0.2.1")
        self.assertTrue(r["accepted"])

if __name__=="__main__":
    unittest.main()
