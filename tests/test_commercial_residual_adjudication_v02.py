
import unittest,importlib.util
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("m",ROOT/"scripts"/"audit_commercial_residual_adjudication_v02.py")
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class T(unittest.TestCase):
    def test_title(self):self.assertEqual(m.norm_title("关于《重大合同》的公告"),"重大合同的公告")
    def test_dates(self):self.assertEqual(m.exchange_dates("2026-01-03",2),["2026-01-01","2026-01-02","2026-01-03","2026-01-04","2026-01-05"])
    def test_url(self):self.assertEqual(m.official_url("finalpage/a.pdf","https://static.cninfo.com.cn/"),"https://static.cninfo.com.cn/finalpage/a.pdf")
    def test_host(self):
        self.assertTrue(m.allowed_host("https://static.cninfo.com.cn/a.pdf",["static.cninfo.com.cn"]))
        self.assertFalse(m.allowed_host("https://example.com/a.pdf",["static.cninfo.com.cn"]))
if __name__=="__main__":unittest.main()
