import unittest
import pandas as pd

from research.deep_oversold_decay_attribution import (
    composition_attribution,
    make_daily_signal_frame,
)


class DeepOversoldDecayAttributionTests(unittest.TestCase):
    def test_daily_equal_weight_collapses_many_events(self):
        e = pd.DataFrame([
            {"signal_date":"20210101","open_excess_gross":1.0,"panic_regime":"A"},
            {"signal_date":"20210101","open_excess_gross":3.0,"panic_regime":"A"},
            {"signal_date":"20210102","open_excess_gross":-1.0,"panic_regime":"B"},
        ])
        d = make_daily_signal_frame(e,"open_excess_gross")
        self.assertEqual(len(d),2)
        self.assertAlmostEqual(d.loc[d.signal_date=="20210101","daily_equal_return"].iloc[0],2.0)

    def test_composition_effect_detects_mix_shift(self):
        rows = []
        # F1: mostly strong bucket A, F2: mostly weak B. Returns within buckets fixed.
        for i in range(8):
            rows.append({"signal_date":f"202101{i+1:02d}","fold":"F1","daily_equal_return":1.0,"panic_regime":"A","event_count":1})
        for i in range(2):
            rows.append({"signal_date":f"202102{i+1:02d}","fold":"F1","daily_equal_return":0.0,"panic_regime":"B","event_count":1})
        for i in range(2):
            rows.append({"signal_date":f"202301{i+1:02d}","fold":"F2","daily_equal_return":1.0,"panic_regime":"A","event_count":1})
        for i in range(8):
            rows.append({"signal_date":f"202302{i+1:02d}","fold":"F2","daily_equal_return":0.0,"panic_regime":"B","event_count":1})
        d = pd.DataFrame(rows)
        r = composition_attribution(d,("panic_regime",),"F1","F2")
        self.assertAlmostEqual(r["observed_date_equal_change"],-0.6)
        self.assertAlmostEqual(r["composition_effect_using_pooled_bucket_means"],-0.6)
        self.assertAlmostEqual(r["residual_within_regime_and_other"],0.0)


if __name__ == "__main__":
    unittest.main()
