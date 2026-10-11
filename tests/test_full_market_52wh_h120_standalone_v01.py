
import unittest, importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"run_full_market_52wh_h120_standalone_v01.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_random_key_deterministic(self):
        r={"symbol":"600000.SH","signal_date":"2024-01-02"}
        self.assertEqual(m.deterministic_random_key(7,"2024-01-03",r),
                         m.deterministic_random_key(7,"2024-01-03",r))
        self.assertNotEqual(m.deterministic_random_key(7,"2024-01-03",r),
                            m.deterministic_random_key(8,"2024-01-03",r))

    def test_calendar_year_returns(self):
        c=pd.DataFrame({"date":["2024-01-02","2024-12-31","2025-12-31"],
                        "equity":[101.0,110.0,121.0]})
        z=m.calendar_year_returns(c,100.0)
        self.assertAlmostEqual(z["2024"],0.10)
        self.assertAlmostEqual(z["2025"],0.10)

    def test_pct_summary(self):
        z=m.pct_summary([1,2,3,4,5])
        self.assertEqual(z["n"],5)
        self.assertAlmostEqual(z["median"],3.0)

    def test_phase1_contract(self):
        cfg={"phase1_contract":{"required_verdict":"PROMISING_HORIZON_CANDIDATE",
                                "required_h120_pass":True,"required_h60_pass":False}}
        s={"decision":{"verdict":"PROMISING_HORIZON_CANDIDATE",
                       "h120":{"horizon_pass":True},"h60":{"horizon_pass":False}}}
        z=m.phase1_gate_check(cfg,s)
        self.assertTrue(all(v["pass"] for v in z.values()))

    def test_gate(self):
        cfg={"standalone_gate":{"sharpe20_min":.5,"mdd20_floor":-.2,
                                "positive_calendar_years_min":2}}
        main={"metrics":{"cagr":.05,"sharpe":.8,"max_drawdown":-.1,
                         "calendar_year_returns":{"2024":.01,"2025":.02,"2026":-.01}}}
        stress={"metrics":{"cagr":.04}}
        rnd={"cagr":{"median":.03,"p10":.01}}
        self.assertEqual(m.standalone_gate(main,stress,rnd,cfg)["status"],
                         "PASS_STANDALONE_PORTFOLIO_GATE")

    def test_same_day_exit_not_reusable_semantics_documented(self):
        src=(ROOT/"scripts"/"run_full_market_52wh_h120_standalone_v01.py").read_text(encoding="utf-8")
        self.assertLess(src.find("# Open mark BEFORE exits"),src.find("# Exact S120 CLOSE exits"))

if __name__=="__main__":
    unittest.main()
