from __future__ import annotations
import importlib.util, json, sys, tempfile, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts"/"run_optimus_steady_state_v074.py"

spec=importlib.util.spec_from_file_location("steady_v074_utf8",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

class Utf8SubprocessTests(unittest.TestCase):
    def test_source_forces_child_utf8(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertIn('env["PYTHONIOENCODING"]="utf-8"',s)
        self.assertIn('env["PYTHONUTF8"]="1"',s)
        self.assertIn('encoding="utf-8"',s)
        self.assertIn('errors="strict"',s)

    def test_run_json_roundtrips_unicode_arrow(self):
        old_root=m.PROJECT_ROOT
        try:
            with tempfile.TemporaryDirectory() as td:
                root=Path(td)
                (root/"scripts").mkdir()
                child=root/"scripts"/"emit_unicode.py"
                child.write_text(
                    'import json\n'
                    'print(json.dumps({"status":"PASS","text":"Tesla↔Robot"},ensure_ascii=False))\n',
                    encoding="utf-8"
                )
                m.PROJECT_ROOT=root
                out=m.run_json("scripts/emit_unicode.py")
                self.assertEqual(out["status"],"PASS")
                self.assertEqual(out["text"],"Tesla↔Robot")
        finally:
            m.PROJECT_ROOT=old_root

    def test_baseline_hash_is_not_changed_by_hotfix(self):
        p=ROOT/"config/research/optimus_steady_state_baseline_v074.json"
        import hashlib
        self.assertEqual(
            hashlib.sha256(p.read_bytes()).hexdigest(),
            "97859e333c144bb0f5812bc5c67c54e6f5b809944df3217a7e2c718b3e1700f5"
        )

if __name__=="__main__":
    unittest.main()
