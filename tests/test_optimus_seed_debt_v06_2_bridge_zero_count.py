from __future__ import annotations
import importlib.util, json, hashlib, sys, tempfile, unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_optimus_seed_debt_v06_apply.py"
spec = importlib.util.spec_from_file_location("v06apply_hotfix", SCRIPT)
m = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = m
spec.loader.exec_module(m)

class BridgeZeroCountGuardTests(unittest.TestCase):
    def test_zero_error_counts_are_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cap = root / "20261009T212806+0800"
            cap.mkdir(parents=True)

            rq = cap / "review_queue.json"
            rq.write_text("[]\n", encoding="utf-8")
            old_root = m.BRIDGE_ROOT
            old_rq_sha = m.REVIEW_QUEUE_SHA256
            try:
                m.BRIDGE_ROOT = root
                m.REVIEW_QUEUE_SHA256 = hashlib.sha256(rq.read_bytes()).hexdigest()

                rows = []
                for eid, exp in m.RAW_EXPECTED.items():
                    rows.append({
                        "evidence_id": eid,
                        "symbol": exp["symbol"],
                        "evidence_level": exp["level"],
                        "tesla_link_level": 0,
                        "source_sha256": exp["source_sha256"],
                        "parser_version": "OPTIMUS_EVIDENCE_ACQUISITION_V0_4_4",
                        "source_type": "CNINFO_PRIMARY_PDF",
                    })
                ep = cap / "evidence_proposals_v0_4_4.jsonl"
                ep.write_text(
                    "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows),
                    encoding="utf-8",
                )

                manifest = {
                    "status": "PASS_OPTIMUS_CLASSIFIER_BRIDGE_V0_4_6",
                    "proposal_count": 7,
                    "parse_error_count": 0,
                    "local_pdf_read_error_count": 0,
                    "classifier_script_sha256": m.EVIDENCE_SCRIPT_SHA256,
                }
                (cap / "manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                found, got_manifest, got_rows = m.find_bridge()
                self.assertEqual(found, cap)
                self.assertEqual(got_manifest["proposal_count"], 7)
                self.assertEqual(got_manifest["parse_error_count"], 0)
                self.assertEqual(got_manifest["local_pdf_read_error_count"], 0)
                self.assertEqual(len(got_rows), 7)
            finally:
                m.BRIDGE_ROOT = old_root
                m.REVIEW_QUEUE_SHA256 = old_rq_sha

if __name__ == "__main__":
    unittest.main()
