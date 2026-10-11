import unittest,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from research.g4_execution_equivalence import *
class T(unittest.TestCase):
    def ds(self):
        out=[]
        rows=[
          ("D","600001.SH","BUY",60000,"A"),
          ("R","000001.SZ","BUY",60000,"B"),
          ("D","600002.SH","BUY",18000,"C"),
          ("R","600001.SH","BUY",60000,"D"),
        ]
        for st,sym,side,n,sk in rows:
            out.append(ShadowOrderDecision(stable_decision_id("20261009",st,sym,side,sk),"20261009",st,sym,side,n,.01,sk))
        return out
    def test_stable_hash_order_independent(self):
        a=deterministic_sample(self.ds(),"EXPANDED")
        b=deterministic_sample(list(reversed(self.ds())),"EXPANDED")
        self.assertEqual(candidate_fingerprint(a),candidate_fingerprint(b))
    def test_canary_one(self):self.assertEqual(len(deterministic_sample(self.ds(),"CANARY")),1)
    def test_expanded_two(self):self.assertEqual(len(deterministic_sample(self.ds(),"EXPANDED")),2)
    def test_unique_symbols(self):
        x=deterministic_sample(self.ds(),"EXPANDED");self.assertEqual(len({r["symbol"] for r in x}),len(x))
    def test_never_over_50k(self):
        self.assertTrue(all(r["pilot_target_notional_rmb"]<=50000 for r in deterministic_sample(self.ds(),"EXPANDED")))
    def test_never_enlarge(self):
        self.assertTrue(all(r["pilot_target_notional_rmb"]<=r["shadow_target_notional_rmb"] for r in deterministic_sample(self.ds(),"EXPANDED")))
    def test_material_excludes_price_and_return(self):
        d=self.ds()[0];m=sampling_material(d).lower()
        self.assertNotIn("price",m);self.assertNotIn("return",m);self.assertNotIn("adv",m);self.assertNotIn("pnl",m)
    def test_duplicate_id_fail_closed(self):
        d=self.ds()[0]
        with self.assertRaises(ValueError):deterministic_sample([d,d],"CANARY")
    def test_single_date_required(self):
        x=self.ds(); d=x[0]
        y=ShadowOrderDecision(d.shadow_decision_id+"x","20261010",d.strategy_id,d.symbol,d.side,d.shadow_target_notional_rmb,.01,"Z")
        with self.assertRaises(ValueError):deterministic_sample([d,y],"EXPANDED")
if __name__=="__main__":unittest.main()
