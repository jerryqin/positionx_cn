from __future__ import annotations
import importlib.util, sys, unittest
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts"/"run_optimus_leisai_canonical_fact_v054_plan.py"

spec=importlib.util.spec_from_file_location("leisai_v054b",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

class PreviewTimeHotfixTests(unittest.TestCase):
    def test_pending_apply_placeholder_removed(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('"canonicalized_at":"PENDING_APPLY"',s)

    def test_now_cst_is_valid_iso_datetime(self):
        x=m.now_cst().isoformat(timespec="seconds")
        parsed=datetime.fromisoformat(x)
        self.assertIsNotNone(parsed.tzinfo)

    def test_preview_comment_preserves_apply_semantics(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("preview timestamp is NOT the",s)
        self.assertIn("V0.5.1 assigns canonicalized_at again at APPLY",s)

if __name__=="__main__":
    unittest.main()
