import unittest, sys
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from research.g5_4m_nearest_lot_economic_replay import *

class T(unittest.TestCase):
    def test_main_nearest_up(self):
        x=nearest_executable_shares("600000.SH",10,10500,.25,20000)
        self.assertEqual(x[0],1000)
    def test_main_nearest_down(self):
        x=nearest_executable_shares("600000.SH",10,9600,.25,20000)
        self.assertEqual(x[0],1000)
    def test_star_min_200(self):
        x=nearest_executable_shares("688001.SH",50,9500,.25,20000)
        self.assertEqual(x[0],200)
    def test_star_increment_one(self):
        x=nearest_executable_shares("688001.SH",50,10550,.25,20000)
        self.assertEqual(x[0],211)
    def test_reject_outside_25pct(self):
        self.assertIsNone(nearest_executable_shares("600000.SH",200,15000,.25,50000))
    def test_cap_can_force_lower_lot(self):
        x=nearest_executable_shares("600000.SH",10,15500,.25,15000)
        self.assertEqual(x[0],1500)
    def test_policy_frozen(self):
        self.assertEqual(POLICY.total_pilot_capital,4_000_000)
        self.assertEqual(POLICY.max_relative_target_error,.25)
        self.assertEqual(POLICY.fidelity_min,.90)
        self.assertEqual(POLICY.max_abs_cagr_drift,.005)
    def test_guardrails(self):
        b={"cagr":.10,"max_drawdown":-.10,"sharpe":1.0}
        l={"cagr":.104,"max_drawdown":-.096,"sharpe":1.09}
        self.assertTrue(drift_pass(b,l)["pass"])
    def test_guardrail_fail(self):
        b={"cagr":.10,"max_drawdown":-.10,"sharpe":1.0}
        l={"cagr":.106,"max_drawdown":-.10,"sharpe":1.0}
        self.assertFalse(drift_pass(b,l)["pass"])
    def test_lot_fidelity_not_tautological(self):
        self.assertAlmostEqual(lot_fidelity(90,100),.90)
        self.assertAlmostEqual(lot_fidelity(80,100),.80)
    def test_fixed_sleeve_preserves_first_common_day_return(self):
        d=pd.DataFrame({"date":["20240102","20240103","20240104"],"equity":[100,110,121]})
        r=pd.DataFrame({"date":["20240103","20240104"],"equity":[105,110.25]})
        z=combine_fixed_initial_sleeves(d,r,300,100,100)
        # Deep: 150*110/100=165; RET: 150*105/100=157.5
        self.assertAlmostEqual(float(z.iloc[0].equity),322.5)
    def test_source_contains_own_exit_match(self):
        s=(ROOT/"scripts/audit_g5_4m_nearest_lot_economic_replay_v02.py").read_text(encoding="utf-8")
        self.assertIn('pos["exit_date"]==date',s)
        self.assertNotIn('for row in exits.get(date,[])',s)
    def test_source_crops_ret_calendar_to_665(self):
        s=(ROOT/"scripts/audit_g5_4m_nearest_lot_economic_replay_v02.py").read_text(encoding="utf-8")
        self.assertIn('len(calendar) != 665',s)
        self.assertIn('d0=min(q5["entry_date"])',s)

if __name__=="__main__":unittest.main()
