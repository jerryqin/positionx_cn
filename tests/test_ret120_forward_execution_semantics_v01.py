import importlib.util
from pathlib import Path
import unittest
import tempfile
import json
import pandas as pd

SCRIPT=Path(__file__).resolve().parents[1]/"scripts"/"audit_ret120_forward_execution_semantics_v01.py"
spec=importlib.util.spec_from_file_location("p4b",SCRIPT); p4b=importlib.util.module_from_spec(spec); spec.loader.exec_module(p4b)

class P4BTests(unittest.TestCase):
    def test_st_name_semantics(self):
        self.assertTrue(p4b.is_st_name("*ST某某")); self.assertTrue(p4b.is_st_name("ST 某某")); self.assertTrue(p4b.is_st_name("S*ST某某")); self.assertFalse(p4b.is_st_name("正常股份"))
    def test_standard_open_blocks_suspension(self):
        self.assertFalse(p4b.standard_open_v1(10,11,1)["fillable"])
    def test_standard_open_blocks_up_limit(self):
        self.assertFalse(p4b.standard_open_v1(11,11,0,0.01)["fillable"])
        self.assertTrue(p4b.standard_open_v1(10.99,11,0,0.01)["fillable"])
    def test_standard_open_blocks_one_price_up_without_limit(self):
        self.assertFalse(p4b.standard_open_v1(11,None,0,0.01,11,11,11,10)["fillable"])
    def test_historical_formula(self):
        ok=pd.Series(dict(is_suspended=False,open=10,high=10.5,low=9.9,close=10.2,pre_close=10,up_limit=11))
        bad=pd.Series(dict(is_suspended=False,open=11,high=11,low=11,close=11,pre_close=10,up_limit=11))
        self.assertTrue(p4b.historical_can_buy_formula(ok)); self.assertFalse(p4b.historical_can_buy_formula(bad))
    def test_review_gate_is_two_turnover_waves(self):
        g=p4b.review_gate(); self.assertEqual(g["completed_trades_target"],60); self.assertEqual(g["calendar_months_target"],12)
    def test_exit_policy_never_uses_last_mark_as_execution(self):
        e=p4b.resolved_forward_policy()["exit"]
        self.assertEqual(e["synthetic_or_last_price_execution"],"FORBIDDEN")
        self.assertIn("first subsequent session",e["if_s120_suspended_or_no_valid_close"])
    def test_guardrails_frozen(self):
        self.assertEqual(p4b.MAX_DELAYED_EXECUTED_FRACTION,0.05); self.assertEqual(p4b.MAX_ABS_CAGR_DRIFT,0.005); self.assertEqual(p4b.MAX_ABS_SHARPE_DRIFT,0.10)
    def test_atomic_json_write_has_runtime_os_dependency(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/"result.json"
            p4b.write_json_atomic(out,{"status":"OK"})
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["status"],"OK")
            self.assertFalse((Path(d)/"result.json.tmp").exists())

if __name__=="__main__": unittest.main()
