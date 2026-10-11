import importlib.util
from pathlib import Path
import unittest
import pandas as pd
import numpy as np

HERE=Path(__file__).resolve()
SCRIPT=HERE.parents[1]/"scripts"/"audit_ret120_vs_deep_incremental_v01.py"
if not SCRIPT.exists():
    SCRIPT=HERE.parents[2]/"payload"/"scripts"/"audit_ret120_vs_deep_incremental_v01.py"
spec=importlib.util.spec_from_file_location("p3",SCRIPT); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class TestP3(unittest.TestCase):
    def test_metric_positive(self):
        d=pd.date_range("2024-01-01",periods=252,freq="D")
        r=pd.Series([0.001]*252)
        z=m.metric_from_returns(d,r)
        self.assertGreater(z["cagr"],0)
        self.assertAlmostEqual(z["max_drawdown"],0.0,places=12)

    def test_fixed_50_50_is_initial_capital_sleeves_not_daily_rebalance(self):
        d=pd.date_range("2024-01-01",periods=520,freq="D")
        a=pd.DataFrame({"date":d,"deep_return":[0.01]+[0.0]*519,"deep_gross":[0.1]*520})
        b=pd.DataFrame({"date":d,"trend_return":[0.0,0.01]+[0.0]*518,"trend_gross":[0.2]*520})
        c,_,_,_=m.fixed_50_50(a,b)
        expected=0.5*1.01+0.5*1.01
        self.assertAlmostEqual(c.combo_nav.iloc[-1],expected,places=12)

    def test_key_overlap(self):
        a=pd.DataFrame({"symbol":["A","B"],"signal_date":["1","2"]})
        b=pd.DataFrame({"symbol":["B","C"],"signal_date":["2","3"]})
        z=m.key_overlap(a,b,["symbol","signal_date"])
        self.assertEqual(z["intersection"],1)
        self.assertAlmostEqual(z["jaccard"],1/3)

    def test_holding_overlap(self):
        d=pd.to_datetime(["2024-01-02","2024-01-03","2024-01-04"])
        a=pd.DataFrame({"symbol":["A"],"entry_date":[d[0]],"exit_date":[d[2]]})
        b=pd.DataFrame({"symbol":["A"],"entry_date":[d[1]],"exit_date":[d[2]]})
        z,_=m.holding_overlap(a,b,d)
        self.assertEqual(z["days_with_same_symbol_active"],2)
        self.assertAlmostEqual(z["fraction_days_with_same_symbol_active"],2/3)

    def test_quantile(self):
        z=m.qtile(pd.Series(range(100)))
        self.assertAlmostEqual(z["median"],49.5)
        self.assertAlmostEqual(z["p10"],9.9)

    def test_deep_v013_economic_equivalence_actual_schema(self):
        s={
            "status":"PASS_DEEP_OVERSOLD_ECONOMIC_EQUIVALENCE_V0_1_3",
            "frozen_event_replay_on_current_local_history":{
                "economic_equivalence_for_alpha2":True
            },
        }
        self.assertTrue(m.deep_economic_equivalence_approved(s))

    def test_deep_wrong_or_missing_nested_key_still_fails_closed(self):
        wrong={
            "status":"PASS_DEEP_OVERSOLD_ECONOMIC_EQUIVALENCE_V0_1_3",
            "frozen_event_replay":{"economic_equivalence_for_alpha2":True},
        }
        missing={"status":"PASS_DEEP_OVERSOLD_ECONOMIC_EQUIVALENCE_V0_1_3"}
        self.assertFalse(m.deep_economic_equivalence_approved(wrong))
        self.assertFalse(m.deep_economic_equivalence_approved(missing))

    def test_deep_exact_replay_status_is_approved_without_equivalence_flag(self):
        s={"status":"PASS_DEEP_OVERSOLD_EXACT_REPLAY_V0_1_3"}
        self.assertTrue(m.deep_economic_equivalence_approved(s))

    def test_no_strategy2_auto_verdict_string(self):
        src=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("does NOT automatically grant Strategy #2",src)
        self.assertNotIn("PASS_STRATEGY_2",src)

if __name__=="__main__": unittest.main()
