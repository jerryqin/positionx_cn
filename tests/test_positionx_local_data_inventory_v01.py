
import unittest, importlib.util
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('m',ROOT/'scripts'/'audit_positionx_local_data_inventory_v01.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
class T(unittest.TestCase):
    def test_family(self):
        self.assertEqual(m.family(Path(r'D:\positionx_data\events\forecast_semantic_corpus\x.pdf')),'EARNINGS_FORECAST_FOCUSED')
    def test_cols(self): self.assertIn('announcementTitle',m.text_cols(['announcementTitle','price']))
    def test_kw(self):
        cfg={'keywords':{'HARD_CONFIRMATION':['中标','合同'],'SOFT_INTENT':['框架协议']}}
        z=m.kw_counts(pd.Series(['中标通知书','重大合同','框架协议']),cfg); self.assertEqual(z['HARD_CONFIRMATION'],2); self.assertEqual(z['SOFT_INTENT'],1)
if __name__=='__main__': unittest.main()
