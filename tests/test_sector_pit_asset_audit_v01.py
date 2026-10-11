import sys, unittest
from pathlib import Path
P=Path(__file__).resolve().parents[1]/'scripts/audit_sector_pit_assets_v01.py'
s=P.read_text(encoding='utf-8'); import re; ns={'re':re}; exec(s[s.index('NAME_KEYS='):s.index('def inspect_catalog')],ns)
class T(unittest.TestCase):
    def test_pit_candidate(self): self.assertEqual(ns['classify'](['symbol','industry_name','valid_from'])[0],'PIT_CANDIDATE')
    def test_current_only(self): self.assertEqual(ns['classify'](['symbol','industry_name'])[0],'CURRENT_ONLY_PIT_UNSAFE')
    def test_no_symbol(self): self.assertEqual(ns['classify'](['industry_name','trade_date'])[0],'INCOMPLETE_NO_SYMBOL')
    def test_score(self): self.assertGreaterEqual(ns['score']('industry_membership',['symbol','industry_code','trade_date']),8)
    def test_qid(self): self.assertEqual(ns['qid']('close'),'"close"')
if __name__=='__main__': unittest.main()
