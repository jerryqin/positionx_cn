import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "freeze_ret120_strategy2_qualification_v01.py"
spec = importlib.util.spec_from_file_location("s2q", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def sample():
    p3 = {
        "status": mod.EXPECTED_P3,
        "trend_family_decision": {"representative": mod.EXPECTED_REP},
        "lineage": {
            "ret120_p1_status": mod.EXPECTED_P1,
            "ret120_p0_status": mod.EXPECTED_P0,
            "deep_status": mod.EXPECTED_DEEP,
        },
        "delta_fixed_50_50": {"vs_deep": {"cagr": .02, "max_drawdown": .01, "sharpe": .2}},
        "random_selection_sensitivity": {
            "fraction_seeds_combo_cagr_gt_deep": .95,
            "fraction_seeds_combo_mdd_better_than_deep": 1.0,
            "fraction_seeds_combo_sharpe_gt_deep": .94,
        },
        "correlation_and_drawdown": {"daily_return_correlation": .52, "drawdown_correlation": .38},
        "executed_trade_overlap": {"intersection": 0, "jaccard": 0.0},
        "holding_overlap": {"mean_symbol_jaccard": .02},
    }
    fam = {"status": mod.EXPECTED_FAMILY}
    return p3, fam


class Tests(unittest.TestCase):
    def test_strong_pass_qualifies(self):
        p3, fam = sample()
        q = mod.classify(p3, fam)
        self.assertEqual(q["status"], "QUALIFIED_STRATEGY_2_HISTORICAL_EVIDENCE_V0_1")

    def test_worse_mdd_fails_closed(self):
        p3, fam = sample(); p3["delta_fixed_50_50"]["vs_deep"]["max_drawdown"] = -1e-6
        with self.assertRaises(RuntimeError): mod.classify(p3, fam)

    def test_lower_sharpe_fails_closed(self):
        p3, fam = sample(); p3["delta_fixed_50_50"]["vs_deep"]["sharpe"] = -1e-6
        with self.assertRaises(RuntimeError): mod.classify(p3, fam)

    def test_random_support_is_recorded_not_posthoc_gate(self):
        p3, fam = sample(); p3["random_selection_sensitivity"]["fraction_seeds_combo_cagr_gt_deep"] = .50
        q = mod.classify(p3, fam)
        self.assertEqual(q["status"], "QUALIFIED_STRATEGY_2_HISTORICAL_EVIDENCE_V0_1")
        self.assertEqual(q["supporting_independence_evidence_not_new_numeric_gate"]["random_fraction_combo_cagr_gt_deep"], .50)

    def test_forward_design_does_not_start_forward(self):
        d = mod.forward_design()
        self.assertEqual(d["scheduler"], "NOT_CREATED")
        self.assertFalse(d["auto_execute"])
        self.assertEqual(len(d["must_resolve_before_forward_collection"]), 3)

    def test_forward_tie_semantics_match_v022(self):
        d = mod.forward_design()
        self.assertIn("symbol ASC", d["signal"]["tie_semantics"])
        self.assertIn("rank(method='first')", d["signal"]["tie_semantics"])
        self.assertEqual(d["signal"]["selected_bucket"], "Q5")


if __name__ == "__main__":
    unittest.main()
