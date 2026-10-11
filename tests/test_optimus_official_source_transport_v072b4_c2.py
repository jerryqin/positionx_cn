from __future__ import annotations
import importlib.util, json, sys, tempfile, unittest
from pathlib import Path
from urllib.error import URLError

ROOT=Path(__file__).resolve().parents[1]
B=ROOT/"scripts"/"run_optimus_external_classifier_v072b.py"
BCFG=json.loads((ROOT/"config/research/optimus_external_classifier_v072b.json").read_text(encoding="utf-8"))
C=ROOT/"scripts"/"run_optimus_candidate_discovery_v072c.py"
CCFG=json.loads((ROOT/"config/research/optimus_candidate_discovery_v072c.json").read_text(encoding="utf-8"))

def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    m=importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name]=m
    spec.loader.exec_module(m)
    return m

b=load(B,"v072b4")
c=load(C,"v072c2")

class TransportHardeningTests(unittest.TestCase):
    def test_sse_transport_mode(self):
        self.assertEqual(
            b.transport_mode("SRC_SSE_OFFICIAL",BCFG),
            "MANUAL_BROWSER_FALLBACK_ON_NETWORK_OR_DYNAMIC_SHELL"
        )

    def test_connection_reset_is_manual_fallback(self):
        e=ConnectionResetError(10054,"reset")
        self.assertTrue(b.is_manual_network_fallback("SRC_SSE_OFFICIAL",e,BCFG))

    def test_urlerror_is_manual_fallback(self):
        self.assertTrue(b.is_manual_network_fallback("SRC_SSE_OFFICIAL",URLError("timeout"),BCFG))

    def test_2996_byte_sseinfo_shell_is_detected(self):
        html=b"<html><head><title>Roadshow</title></head><body><div id='app'></div></body></html>"
        # pad to exact observed raw size with whitespace
        html=html + b" "*(2996-len(html))
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"raw.html"; p.write_bytes(html)
            reason=b.dynamic_shell_reason(
                p,"https://roadshow.sseinfo.com/stayTuned/41251","text/html",
                {"entity_name":"无锡信捷电气股份有限公司"},BCFG
            )
            self.assertIsNotNone(reason)
            self.assertIn("DYNAMIC_HTML_SHELL",reason)

    def test_substantive_large_html_not_shell(self):
        body=("人形机器人关节核心零部件规模化批量供货。"*500).encode("utf-8")
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"raw.html"; p.write_bytes(b"<html><body>"+body+b"</body></html>")
            reason=b.dynamic_shell_reason(
                p,"https://roadshow.sseinfo.com/stayTuned/41251","text/html",
                {"entity_name":"无锡信捷电气股份有限公司"},BCFG
            )
            self.assertIsNone(reason)

    def test_c_runtime_policy_does_not_freeze_latest_manifest(self):
        r=CCFG["runtime_dependency_policy"]
        self.assertTrue(r["freeze_classifier_code_and_config_hashes"])
        self.assertTrue(r["latest_classifier_error_count_must_be_zero"])
        self.assertTrue(r["latest_classifier_queue_count_may_be_nonzero"])
        self.assertTrue(r["latest_classifier_manifest_hash_is_not_frozen_after_activation"])

    def test_c_still_read_only(self):
        s=CCFG["safety"]
        self.assertFalse(s["registry_mutation"])
        self.assertFalse(s["canonicalization_mutation"])
        self.assertFalse(s["theme_stage_mutation"])

if __name__=="__main__":
    unittest.main()
