import unittest,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from research.pilot_subledger import *

class T(unittest.TestCase):
    def test_remark(self):
        self.assertEqual(expected_remark("2026-10-08","abcdef123456"),"PX|20261008|abcdef12")
    def test_buy_mark_pnl(self):
        f=[Fill("t1",1,"20261008","A.SH","BUY",10,100,1000)]
        x=reconstruct_subledger(f,{"A.SH":11})
        self.assertEqual(x["marked_gross_rmb"],1100)
        self.assertEqual(x["total_pnl_pre_fee_rmb"],100)
        self.assertEqual(x["cashflow_mark_pnl_rmb"],100)
    def test_partial_sell(self):
        f=[Fill("t1",1,"20261008","A.SH","BUY",10,100,1000),
           Fill("t2",2,"20261009","A.SH","SELL",12,40,480)]
        x=reconstruct_subledger(f,{"A.SH":11})
        self.assertEqual(x["open_positions"]["A.SH"]["shares"],60)
        self.assertEqual(x["realized_pnl_pre_fee_rmb"],80)
        self.assertEqual(x["unrealized_pnl_pre_fee_rmb"],60)
        self.assertEqual(x["total_pnl_pre_fee_rmb"],140)
    def test_sell_external_shares_forbidden(self):
        f=[Fill("t1",1,"20261008","A.SH","BUY",10,100,1000),
           Fill("t2",2,"20261009","A.SH","SELL",12,200,2400)]
        with self.assertRaises(ValueError):reconstruct_subledger(f,{"A.SH":12})
    def test_loss_ladder(self):
        self.assertEqual(loss_ladder(-19999)["state"],"NORMAL")
        self.assertEqual(loss_ladder(-20000)["state"],"WARNING")
        self.assertEqual(loss_ladder(-30000)["state"],"STOP_NEW_ENTRIES")
        self.assertEqual(loss_ladder(-40000)["state"],"EMERGENCY_EXIT_PLAN_REQUIRED")
        self.assertEqual(loss_ladder(-50000)["state"],"BUDGET_BREACH")
    def test_gross_guard(self):
        self.assertEqual(gross_guard(100000,50000)["state"],"PASS")
        self.assertEqual(gross_guard(150000,1)["state"],"BLOCK_NEW_ENTRY_PLANNED_GROSS")
        self.assertEqual(gross_guard(179000,2000)["state"],"BLOCK_HARD_GROSS")
    def test_missing_mark_reported(self):
        f=[Fill("t1",1,"20261008","A.SH","BUY",10,100,1000)]
        x=reconstruct_subledger(f,{})
        self.assertEqual(x["missing_marks"],["A.SH"])
        self.assertIsNone(x["cashflow_mark_pnl_rmb"])

if __name__=="__main__":unittest.main()
