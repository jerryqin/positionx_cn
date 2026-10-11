import unittest,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from research.g4_open_confirmation import *
class T(unittest.TestCase):
    def test_deep_full_capacity(self):
        x=deep_open_allocation(1_000_000,[],{},[Candidate("A",True),Candidate("B",True)])
        self.assertAlmostEqual(x["equity_open"],6_000_000)
        self.assertAlmostEqual(x["desired_each"],60_000)
        self.assertAlmostEqual(x["notional_by_symbol"]["A"],60_000)
    def test_deep_prorata_when_gross_binds(self):
        # 1.5m open MV leaves 300k room in a 6m-ish sleeve; ten 1% tickets must scale.
        pos=[Position("H",150_000)]
        x=deep_open_allocation(750_000,pos,{"H":10.0},[Candidate(str(i),True) for i in range(10)])
        self.assertLess(x["scale"],1.0)
        vals=set(round(v,8) for v in x["notional_by_symbol"].values())
        self.assertEqual(len(vals),1)
    def test_deep_unfillable_excluded_from_denominator(self):
        x=deep_open_allocation(1_000_000,[],{},[Candidate("A",True),Candidate("B",False)])
        self.assertEqual(x["fillable_new_count"],1)
        self.assertIn("A",x["notional_by_symbol"]);self.assertNotIn("B",x["notional_by_symbol"])
    def test_ret120_symbol_asc(self):
        c=[Candidate("C",True),Candidate("A",True),Candidate("B",True)]
        x=ret120_open_allocation(1_000_000,[],{},c)
        self.assertEqual(list(x["notional_by_symbol"]),["A","B","C"])
    def test_ret120_slot_bound(self):
        pos=[Position(f"H{i}",1.0) for i in range(29)]
        opens={p.symbol:1.0 for p in pos}
        x=ret120_open_allocation(1_000_000,pos,opens,[Candidate("A",True),Candidate("B",True)])
        self.assertEqual(x["open_slots"],1);self.assertEqual(x["selected_count"],1)
    def test_pilot_never_enlarges(self):
        self.assertEqual(pilot_target(18_000),18_000);self.assertEqual(pilot_target(60_000),50_000)
    def test_zero_pilot(self):self.assertEqual(pilot_target(0),0)
if __name__=="__main__":unittest.main()
