
import unittest, importlib.util
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"recover_cninfo_blank_query_exact_date_pilot_v011.py")
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
class T(unittest.TestCase):
    def test_safe(self): self.assertEqual(m.safe_text(pd.NA),"")
    def test_find(self):
        df=pd.DataFrame(columns=["announcementId"]); self.assertEqual(m.find_col(df,["announcement_id","announcementId"]),"announcementId")
    def test_key(self): self.assertEqual(m.key("2020-01-01","sse"),"2020-01-01__sse")
    def test_body(self):
        cfg={"cninfo":{"page_size":30}}
        s=m.body(cfg,"2020-01-01","sse","sh",1).decode()
        self.assertIn("searchkey=",s); self.assertIn("category=",s)
if __name__=="__main__": unittest.main()
