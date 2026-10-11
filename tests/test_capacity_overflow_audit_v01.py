import unittest
import pandas as pd
from research import capacity_overflow_audit as e
class T(unittest.TestCase):
    def test_capacity_reason(self): self.assertTrue(e.is_capacity_block('BLOCKED','MAX_GROSS_REACHED'))
    def test_hygiene_not_capacity(self): self.assertFalse(e.is_capacity_block('EXCLUDED_SIGNAL_HYGIENE','ST'))
    def test_entered(self): self.assertEqual(e.classify_signal('ENTERED','', '20261009',10.0),'ENTERED')
    def test_pending(self): self.assertEqual(e.classify_signal('PENDING_ENTRY','',None,None),'PENDING')
    def test_blocked_capacity(self): self.assertEqual(e.classify_signal('BLOCKED_CAPACITY','MAX_POSITIONS',None,None),'BLOCKED_CAPACITY')
    def test_next_session(self): self.assertEqual(e.next_session(['20261008','20261009','20261012'],'20261008'),'20261009')
    def test_horizon(self): self.assertEqual(e.nth_subsequent_session(['20261008','20261009','20261012'],'20261008',2),'20261012')
    def test_active_at_open(self):
        x=pd.DataFrame([{'symbol':'A','entry_date':'20261008','exit_date':None},{'symbol':'B','entry_date':'20261009','exit_date':None},{'symbol':'C','entry_date':'20261001','exit_date':'20261009'}]); y=e.active_at_open(x,'20261009'); self.assertEqual(set(y.symbol),{'A','C'})
    def test_return(self): self.assertAlmostEqual(e.safe_return(10,11),.1)
    def test_aggregate(self):
        m=e.aggregate_returns(pd.DataFrame({'gross_return':[.1,-.1,.2]})); self.assertEqual(m['n'],3); self.assertAlmostEqual(m['win_rate'],2/3)
if __name__=='__main__': unittest.main()
