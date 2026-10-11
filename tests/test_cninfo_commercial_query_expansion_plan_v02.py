
import unittest, importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"plan_cninfo_commercial_query_expansion_v02.py")
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_existing_keyword(self):
        self.assertTrue(m.has_any("关于公司中标项目的公告",["中标","重大合同"]))
        self.assertFalse(m.has_any("关于收到客户项目通知的公告",["中标","重大合同"]))
    def test_greedy(self):
        cfg={
            "existing_queries":["中标"],
            "candidate_query_terms":{"HARD_CONFIRMATION":["中选","入围"],"SOFT_INTENT":[]},
            "selection":{"min_support":1,"max_terms":2,"target_gap_coverage":1.0}
        }
        df=pd.DataFrame({"title_norm":["公司中选公告","项目入围公告","公司中选及入围"]})
        s,u,_=m.greedy_cover(df,cfg)
        self.assertEqual(len(s),2)
        self.assertEqual(len(u),0)
    def test_na_title(self):
        self.assertEqual(m.norm_title(pd.NA),"")
    def test_rank_stable(self):
        self.assertEqual(m.deterministic_rank("x"),m.deterministic_rank("x"))

if __name__=="__main__": unittest.main()
