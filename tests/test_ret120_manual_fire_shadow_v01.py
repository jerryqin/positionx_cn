
import unittest, json, tempfile
from pathlib import Path
import pandas as pd
from research import ret120_manual_fire_shadow as e

class T(unittest.TestCase):
    def test_classify_open(self):
        self.assertEqual(e.classify_open(10.0,9.9,10.1,9.7),"FIRE_MANUAL")
        self.assertEqual(e.classify_open(10.2,9.9,10.1,9.7),"CANCEL_WAIT_PULLBACK")
        self.assertEqual(e.classify_open(9.8,9.9,10.1,9.7),"CANCEL_WAIT_RECOVERY")
        self.assertEqual(e.classify_open(9.6,9.9,10.1,9.7),"CANCEL_INVALIDATED")

    def test_recommendation_id_deterministic(self):
        r={"symbol":"000001.SZ","lane":"MOMENTUM","lane_rank":1,
           "breakout_level_raw":10.0,"stop_raw":9.7,"open_valid_low":9.9,"open_valid_high":10.05}
        a=e.recommendation_id(r,"2026-09-30","2026-10-08","abc")
        b=e.recommendation_id(r,"2026-09-30","2026-10-08","abc")
        self.assertEqual(a,b)

    def test_sizing_never_enlarges(self):
        r=pd.Series({"lane":"MOMENTUM","stop_raw":9.8,"adv20_amount":1e9,"preview_shares":1000})
        s={"board_lot":100,"adv20_participation_max":.01,
           "momentum_risk_fraction_nav":.0025,"mean_reversion_risk_fraction_nav":.0015,
           "momentum_max_weight":.05,"mean_reversion_max_weight":.03,
           "never_enlarge_beyond_prepare_preview_shares":True}
        z=e.recalc_structural_size(r,10.0,1_500_000,s)
        self.assertLessEqual(z["suggested_shares"],1000)
        self.assertEqual(z["suggested_shares"]%100,0)

    def test_hash_chain(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"x.jsonl"
            a=e.append_hash_chain_jsonl(p,{"a":1})
            b=e.append_hash_chain_jsonl(p,{"a":2})
            self.assertEqual(b["prev_event_hash"],a["event_hash"])
            self.assertNotEqual(a["event_hash"],b["event_hash"])

if __name__=="__main__":
    unittest.main()
