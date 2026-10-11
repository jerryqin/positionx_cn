from __future__ import annotations
import importlib.util, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts/run_optimus_leisai_canonical_fact_v054_apply.py"

class ApplyReadOnlyHotfixTests(unittest.TestCase):
    def test_read_only_schema_check_does_not_init_by_default(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("allow_init:bool=False",s)
        self.assertIn("if allow_init:",s)

    def test_status_allows_only_steady_baseline_drift(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("allow_steady_baseline_drift=True",s)
        self.assertIn('key=="steady_baseline"',s)

    def test_check_knows_already_applied_state(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("APPLY_CHECK_ALREADY_APPLIED",s)
        self.assertIn("validate_state(state,cfg[\"expected_post\"],pre=False)",s)

if __name__=="__main__":
    unittest.main()
