
import unittest,tempfile
from pathlib import Path
import pandas as pd
from research import ret120_shadow_review as e
class T(unittest.TestCase):
    def test_first_terminal_decision(self):
        x=pd.DataFrame([{"recommendation_id":"r1","recorded_at":"2026-01-01T09:00","human_decision":"SKIP"},{"recommendation_id":"r1","recorded_at":"2026-01-01T09:01","human_decision":"APPROVE","human_fill_price":10,"human_fill_shares":100},{"recommendation_id":"r1","recorded_at":"2026-01-01T09:02","human_decision":"REJECT"}]);d=e.first_terminal_decisions(x);self.assertEqual(d.iloc[0]["canonical_human_decision"],"APPROVE");self.assertTrue(bool(d.iloc[0]["decision_conflict"]))
    def test_gate_canonical_identical_duplicates(self):
        x=pd.DataFrame([{"recommendation_id":"r1","_gate_run":"a","fire_state":"FIRE_MANUAL","actual_open":10,"suggested_shares":100,"suggested_notional":1000,"open_valid_low":9.9,"open_valid_high":10.1,"stop_raw":9.7,"lane":"MOMENTUM","symbol":"000001.SZ"},{"recommendation_id":"r1","_gate_run":"b","fire_state":"FIRE_MANUAL","actual_open":10,"suggested_shares":100,"suggested_notional":1000,"open_valid_low":9.9,"open_valid_high":10.1,"stop_raw":9.7,"lane":"MOMENTUM","symbol":"000001.SZ"}]);y=e.canonicalize_gate_rows(x);self.assertEqual(len(y),1);self.assertEqual(y.iloc[0]["_gate_run"],"b")
    def test_gate_conflict_fail_closed(self):
        x=pd.DataFrame([{"recommendation_id":"r1","_gate_run":"a","fire_state":"FIRE_MANUAL","actual_open":10,"suggested_shares":100,"suggested_notional":1000,"open_valid_low":9.9,"open_valid_high":10.1,"stop_raw":9.7,"lane":"MOMENTUM","symbol":"000001.SZ"},{"recommendation_id":"r1","_gate_run":"b","fire_state":"CANCEL_WAIT_PULLBACK","actual_open":10.2,"suggested_shares":0,"suggested_notional":0,"open_valid_low":9.9,"open_valid_high":10.1,"stop_raw":9.7,"lane":"MOMENTUM","symbol":"000001.SZ"}]);
        with self.assertRaises(RuntimeError):e.canonicalize_gate_rows(x)
    def test_account_entries(self):
        g=pd.DataFrame([{"recommendation_id":"r1","fire_state":"FIRE_MANUAL","symbol":"A","lane":"MOMENTUM","actual_open":10,"suggested_shares":100},{"recommendation_id":"r2","fire_state":"FIRE_MANUAL","symbol":"B","lane":"MOMENTUM","actual_open":20,"suggested_shares":100}]);d=pd.DataFrame([{"recommendation_id":"r1","recorded_at":"2026-01-01T09:01","human_decision":"APPROVE","human_fill_price":10.1,"human_fill_shares":100},{"recommendation_id":"r2","recorded_at":"2026-01-01T09:01","human_decision":"REJECT","human_fill_price":None,"human_fill_shares":None}]);a,_=e.build_account_entries(g,d);self.assertEqual((a["account"]=="SYSTEM_ALL_FIRE").sum(),2);self.assertEqual((a["account"]=="HUMAN_SELECTION_SHADOW").sum(),1)
    def test_weighted_average_cost(self):
        s=e.LiveState();e.apply_long_only_event(s,"ENTRY",10,100,1);e.apply_long_only_event(s,"ADD",12,100,1);self.assertAlmostEqual(s.avg_cost,11);e.apply_long_only_event(s,"REDUCE",13,100,1);self.assertAlmostEqual(s.realized_pnl,197);self.assertEqual(s.shares,100);self.assertAlmostEqual(s.fees,3)
    def test_oversell_blocked(self):
        s=e.LiveState(shares=100,avg_cost=10)
        with self.assertRaises(RuntimeError):e.apply_long_only_event(s,"REDUCE",11,200,0)
    def test_exit_must_close(self):
        s=e.LiveState(shares=100,avg_cost=10)
        with self.assertRaises(RuntimeError):e.apply_long_only_event(s,"EXIT",11,50,0)
    def test_cohort_metrics(self):
        x=pd.DataFrame({"gross_return":[.1,-.05,.2]});m=e.cohort_metrics(x);self.assertEqual(m["n"],3);self.assertAlmostEqual(m["win_rate"],2/3)
    def test_hash_chain(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"x.jsonl";a=e.append_hash_chain_jsonl(p,{"a":1});b=e.append_hash_chain_jsonl(p,{"a":2});self.assertEqual(b["prev_event_hash"],a["event_hash"])

    def test_initial_entry_fee_imported_and_net_pnl(self):
        g=pd.DataFrame([{"recommendation_id":"r1","fire_state":"FIRE_MANUAL","symbol":"A","lane":"MOMENTUM","fire_date":"2026-01-02","actual_open":10,"suggested_shares":100}])
        d=pd.DataFrame([{"recommendation_id":"r1","recorded_at":"2026-01-02T09:31","human_decision":"APPROVE","human_fill_price":10,"human_fill_shares":100,"human_fill_fees":5}])
        ev,pos,real=e.human_live_from_ledgers(g,d,pd.DataFrame(),{"A":11})
        self.assertAlmostEqual(float(pos.iloc[0]["fees"]),5)
        self.assertAlmostEqual(float(pos.iloc[0]["total_pnl"]),95)

    def test_old_decision_without_fee_backward_compatible(self):
        x=pd.DataFrame([{"recommendation_id":"r1","recorded_at":"2026-01-01T09:01","human_decision":"APPROVE","human_fill_price":10,"human_fill_shares":100}])
        d=e.first_terminal_decisions(x)
        self.assertEqual(float(d.iloc[0]["human_fill_fees"]),0.0)

if __name__=="__main__":unittest.main()
