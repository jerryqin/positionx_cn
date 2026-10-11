import unittest,sys
from pathlib import Path
from datetime import datetime,timezone,timedelta
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from research.g4_manual_approval import *

def good():
    return {
      "status":"PASS_G4_ISOLATED_PREVIEW_V0_4",
      "shadow":{"shadow_decision_id":"abc","signal_date":"20261008","strategy_id":"S","symbol":"A.SH",
                "stage":"CANARY","confirmed_entry_trade_date":"2026-10-09",
                "confirmed_shadow_notional_rmb":60000,"pilot_target_notional_rmb":50000},
      "preview":{"status":"ORDER_PREVIEW","notional":49000},
      "evidence":{"pass":True,"requested_pilot_notional_rmb":50000,"confirmed_shadow_notional_rmb":60000},
      "manual_approval_payload":{"shadow_decision_id":"abc","symbol":"A.SH","strategy_id":"S",
                 "preview_intent_key":"1234567890abcdef","preview_side":"BUY","preview_volume":1000,
                 "preview_limit_price":49.0,"preview_notional_rmb":49000,"status":"AWAIT_MANUAL_APPROVAL"},
      "safety":{"production_plan_written":False,"live_enabled":False,"auto_execute":False}
    }

class T(unittest.TestCase):
    def test_bound(self):
        b=canonical_bound_fields(good(),"f"*64)
        self.assertEqual(b["shadow_decision_id"],"abc")
        self.assertEqual(b["preview_notional_rmb"],49000)
    def test_token_stable(self):
        b=canonical_bound_fields(good(),"f"*64)
        self.assertEqual(approval_token(b),approval_token(dict(reversed(list(b.items())))))
    def test_bad_preview_rejected(self):
        x=good();x["status"]="BLOCKED"
        with self.assertRaises(ValueError):canonical_bound_fields(x,"f"*64)
    def test_unsafe_flags_rejected(self):
        x=good();x["safety"]["live_enabled"]=True
        with self.assertRaises(ValueError):canonical_bound_fields(x,"f"*64)
    def test_requested_over_shadow_rejected(self):
        x=good();x["evidence"]["requested_pilot_notional_rmb"]=70000
        with self.assertRaises(ValueError):canonical_bound_fields(x,"f"*64)
    def test_freshness(self):
        now=datetime.now(timezone.utc)
        self.assertTrue(freshness(now-timedelta(seconds=60),now)["fresh"])
        self.assertFalse(freshness(now-timedelta(seconds=301),now)["fresh"])
    def test_daily_limits(self):
        self.assertEqual(STAGE_DAILY_LIMIT["CANARY"],1);self.assertEqual(STAGE_DAILY_LIMIT["EXPANDED"],2)
if __name__=="__main__":unittest.main()
