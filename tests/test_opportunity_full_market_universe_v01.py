
import unittest,importlib.util
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"build_opportunity_full_market_universe_v01.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

CFG={"eligibility":{
    "sh_prefixes":["600","601","603","605","688","689"],
    "sz_prefixes":["000","001","002","003","300","301"]
}}

class T(unittest.TestCase):
    def test_a_share_symbol(self):
        for s in ["600000.SH","601318.SH","603986.SH","605001.SH","688041.SH","000001.SZ","002050.SZ","300308.SZ","301080.SZ"]:
            self.assertTrue(m.is_a_share_symbol(s,CFG),s)
        for s in ["900901.SH","200002.SZ","510300.SH","159915.SZ","113000.SH","920106.BJ"]:
            self.assertFalse(m.is_a_share_symbol(s,CFG),s)

    def test_build_universe_filters_suspend_and_zero_amount(self):
        raw=pd.DataFrame({
            "trade_date":pd.to_datetime(["2026-01-01"]*4),
            "symbol":["600000.SH","000001.SZ","002050.SZ","510300.SH"],
            "open":[10]*4,"high":[11]*4,"low":[9]*4,"close":[10]*4,"pre_close":[10]*4,
            "volume":[100,100,100,100],"amount":[1000,1000,0,1000],
            "suspend_flag":[0,1,0,0],"source":["x"]*4
        })
        ann,u=m.build_universe(raw,CFG)
        self.assertEqual(u["symbol"].tolist(),["600000.SH"])

    def test_required_columns(self):
        self.assertIn("amount",m.required_columns())
        self.assertIn("suspend_flag",m.required_columns())

if __name__=="__main__":
    unittest.main()
