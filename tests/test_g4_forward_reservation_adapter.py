import unittest,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from research.g4_forward_reservation_adapter import *
class T(unittest.TestCase):
    def test_unique_status_solution(self):self.assertEqual(infer_pending_statuses({"PENDING":3,"BLOCKED":1},3),["PENDING"])
    def test_zero_pending(self):self.assertEqual(infer_pending_statuses({"ENTERED":3,"BLOCKED":1},0),[])
    def test_ambiguous_status_fails(self):
        with self.assertRaises(RuntimeError):infer_pending_statuses({"A":1,"B":1},1)
    def test_no_solution_fails(self):
        with self.assertRaises(RuntimeError):infer_pending_statuses({"A":2,"B":3},1)
    def sample(self):
        return [
          PendingSignal(DEEP_ID,"600001.SH","20261008","PENDING",None,"forward_signals"),
          PendingSignal(RET120_ID,"000001.SZ","20261008","PENDING",None,"ret120_forward_signals"),
          PendingSignal(DEEP_ID,"600002.SH","20261008","PENDING",None,"forward_signals"),
          PendingSignal(RET120_ID,"600001.SH","20261008","PENDING",None,"ret120_forward_signals"),
        ]
    def test_canary_one(self):self.assertEqual(len(reserve(self.sample(),"CANARY")),1)
    def test_expanded_two(self):self.assertEqual(len(reserve(self.sample(),"EXPANDED")),2)
    def test_unique_symbols(self):
        x=reserve(self.sample(),"EXPANDED");self.assertEqual(len({r["symbol"] for r in x}),len(x))
    def test_order_invariant(self):
        a=reserve(self.sample(),"EXPANDED");b=reserve(list(reversed(self.sample())),"EXPANDED")
        self.assertEqual(fingerprint(a),fingerprint(b))
    def test_notional_not_created_at_reservation(self):
        for r in reserve(self.sample(),"EXPANDED"):
            self.assertIsNone(r["confirmed_shadow_notional_rmb"]);self.assertIsNone(r["pilot_target_notional_rmb"])
    def test_hash_material_not_price_based(self):
        x=self.sample()[0];m=reservation_hash(x)
        self.assertEqual(len(m),64)
if __name__=="__main__":unittest.main()
