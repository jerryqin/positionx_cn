import unittest
import pandas as pd
from research.trend_edge_scan import assign_quintiles, monotonicity_score, fast_reject

class TrendEdgeScanTests(unittest.TestCase):
    def test_quintiles_are_cross_sectional_and_balanced(self):
        x=pd.Series(range(100),dtype=float)
        q=assign_quintiles(x)
        self.assertEqual(q.value_counts().to_dict(),{'Q1':20,'Q2':20,'Q3':20,'Q4':20,'Q5':20})
    def test_monotonicity_is_one_for_increasing_means(self):
        self.assertAlmostEqual(monotonicity_score({'Q1':1,'Q2':2,'Q3':3,'Q4':4,'Q5':5}),1.0)
    def test_fast_reject_candidate_requires_cross_fold_persistence(self):
        full={str(h):{'q5_minus_q1':0.01,'monotonicity':0.9} for h in (20,60,120)}
        folds={f:{str(h):{'q5_minus_q1':0.01} for h in (20,60,120)} for f in ('F1','F2','F3')}
        r=fast_reject({'full_sample':full,'folds':folds})
        self.assertTrue(r['fast_reject_candidate'])
if __name__=='__main__': unittest.main()
