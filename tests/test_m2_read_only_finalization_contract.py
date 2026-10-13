from __future__ import annotations
import ast, hashlib, importlib.util, json, tempfile, unittest
from pathlib import Path
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]

def load_doctor():
    p=ROOT/"scripts/run_positionx_doctor.py"
    spec=importlib.util.spec_from_file_location("positionx_doctor_m2_final",p)
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

class _DB:
    def __init__(self):
        self.rows=[
            {"trade_date":"2026-10-09","open":1.0,"high":2.0,"low":0.5,"close":1.5,"volume":100.0},
            {"trade_date":"2026-10-10","open":1.5,"high":2.5,"low":1.0,"close":2.0,"volume":120.0},
        ]
    def load_daily_bars(self,symbol,bars): return list(self.rows[-bars:])

class M2ReadOnlyFinalizationContractTests(unittest.TestCase):
    def test_history_refresh_false_is_hard_read_only_boundary(self):
        from core.data import MarketDataService
        svc=object.__new__(MarketDataService); svc.db=_DB(); svc.update_daily=Mock(side_effect=AssertionError("unexpected update"))
        df=MarketDataService.history(svc,"000001.SZ",bars=260,refresh=False)
        self.assertEqual(len(df),2); svc.update_daily.assert_not_called()

    def test_history_refresh_true_retains_explicit_stateful_path(self):
        from core.data import MarketDataService
        svc=object.__new__(MarketDataService); svc.db=_DB(); svc.update_daily=Mock(return_value=None)
        MarketDataService.history(svc,"000001.SZ",bars=260,refresh=True)
        svc.update_daily.assert_called_once_with("000001.SZ")

    def test_durable_m2_evidence_is_valid_for_candidate_sources(self):
        doctor=load_doctor()
        cfg=json.loads((ROOT/"config/foundation/positionx_doctor_v01.json").read_text(encoding="utf-8"))
        result=doctor.validate_m2_read_only_evidence(ROOT,cfg)
        self.assertTrue(result["valid"],result)

    def test_durable_evidence_fails_closed_on_source_hash_drift(self):
        doctor=load_doctor()
        with tempfile.TemporaryDirectory() as td:
            r=Path(td); (r/"x").mkdir(); (r/"x/a.txt").write_text("A",encoding="utf-8")
            h=hashlib.sha256((r/"x/a.txt").read_bytes()).hexdigest()
            evidence={"schema":"POSITIONX_CN_QMT_M2_READ_ONLY_EVIDENCE_V1",
                      "live_validation_status":"PASS_POSITIONX_CN_QMT_M2B_FULL_QMT_LIVE_READ_ONLY_VALIDATION_V2",
                      "actual_connections":{"qmt_market_connected":True,"broker_connected":True},
                      "domains":{k:True for k in ("market_data","account","positions","orders","trades")},
                      "order_actions_executed":False,"privacy_redaction_proven":True,
                      "source_bindings":{"x/a.txt":h}}
            (r/"e.json").write_text(json.dumps(evidence),encoding="utf-8")
            cfg={"m2_read_only_evidence_file":"e.json"}
            self.assertTrue(doctor.validate_m2_read_only_evidence(r,cfg)["valid"])
            (r/"x/a.txt").write_text("DRIFT",encoding="utf-8")
            self.assertFalse(doctor.validate_m2_read_only_evidence(r,cfg)["valid"])

    def test_no_hidden_direct_legacy_market_history_read_remains(self):
        service=(ROOT/"core/service.py").read_text(encoding="utf-8")
        self.assertNotIn("self.market.history(",service)
        self.assertEqual(service.count("self.market.refresh_completed_daily_bar("),1)

    def test_residual_broker_calls_are_lifecycle_or_execution_only(self):
        service=(ROOT/"core/service.py").read_text(encoding="utf-8")
        tree=ast.parse(service); calls=[]
        for n in ast.walk(tree):
            if isinstance(n,ast.Call):
                try:s=ast.unparse(n.func)
                except Exception:continue
                if s.startswith("self.broker."): calls.append(s)
        self.assertEqual(sorted(set(calls)),["self.broker.cancel","self.broker.connect","self.broker.disconnect"])

    def test_scheduler_has_no_legacy_market_read_surface(self):
        scheduler=(ROOT/"core/scheduler.py").read_text(encoding="utf-8")
        self.assertNotIn("self.service.market.",scheduler)

    def test_evidence_manifest_contains_no_sensitive_detail_fields(self):
        text=(ROOT/"config/foundation/qmt_m2_read_only_evidence_v1.json").read_text(encoding="utf-8").lower()
        for forbidden in ("account_id","position_symbols","order_details","trade_details","asset_values"):
            self.assertNotIn(forbidden,text)

if __name__=="__main__": unittest.main()
