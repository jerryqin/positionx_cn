from __future__ import annotations
import importlib.util, sys, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

def load(name,rel):
    spec=importlib.util.spec_from_file_location(name,ROOT/rel)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod

orch=load("daily_ops_orchestrator_v2_candidate","scripts/run_daily_ops_orchestrator_v1.py")
wd=load("daily_ops_watchdog_v2_candidate","scripts/run_daily_ops_watchdog_v1.py")

class ShadowOpsDay1HotfixV2Tests(unittest.TestCase):
    def test_a_share_status_current_accepts_committed_current_day(self):
        obj={"state":{"last_processed_date":"20261014","sessions":[{"trade_date":"20261014"}]},"catalog_daily":{"max_date":"2026-10-14"}}
        self.assertTrue(orch.a_share_status_current(obj,"20261014"))
    def test_a_share_status_current_rejects_stale_state(self):
        obj={"state":{"last_processed_date":"20261013","sessions":[{"trade_date":"20261014"}]},"catalog_daily":{"max_date":"2026-10-14"}}
        self.assertFalse(orch.a_share_status_current(obj,"20261014"))
    def test_a_share_status_current_rejects_stale_catalog(self):
        obj={"state":{"last_processed_date":"20261014","sessions":[{"trade_date":"20261014"}]},"catalog_daily":{"max_date":"2026-10-13"}}
        self.assertFalse(orch.a_share_status_current(obj,"20261014"))
    def test_a_share_status_current_requires_session_record(self):
        obj={"state":{"last_processed_date":"20261014","sessions":[]},"catalog_daily":{"max_date":"2026-10-14"}}
        self.assertFalse(orch.a_share_status_current(obj,"20261014"))
    def test_eod_scope_covers_full_day_closure(self):
        required={
            "HEALTH_PREOPEN","BROKER_RECONCILE_PREOPEN","PIT_PREOPEN","OPTIMUS_STATUS","OPTIMUS_ACCEPTANCE","OPTIMUS_ATTENTION","G4_OPEN_CHECK","WATCHDOG_MORNING",
            "BROKER_RECONCILE_PRECLOSE","A_SHARE_DAILY_UPDATE","DEEP_FORWARD","RET120_FORWARD","PORTFOLIO_SHADOW","CAPACITY_PROBE","G4_RESERVATION_INSPECT","PILOT_READINESS","BROKER_RECONCILE_FINAL","WATCHDOG_POSTCLOSE",
            "PIT_POSTCLOSE","DAILY_REPORT"
        }
        self.assertTrue(required.issubset(set(wd.WATCHDOG_GROUPS["eod"])))
    def test_eod_missing_report_artifact_is_red_condition(self):
        bad=wd.apply_scope_requirements("eod",[],False)
        self.assertTrue(any(x.get("task_id")=="DAILY_REPORT_ARTIFACT" for x in bad))
    def test_non_eod_does_not_require_report_artifact(self):
        self.assertEqual(wd.apply_scope_requirements("postclose",[],False),[])
    def test_installer_allows_battery_without_enabling_start_when_available(self):
        text=(ROOT/"scripts"/"install_daily_ops_scheduler_v1.ps1").read_text(encoding="utf-8")
        self.assertIn("-AllowStartIfOnBatteries",text)
        self.assertIn("-DontStopIfGoingOnBatteries",text)
        self.assertNotIn("StartWhenAvailable",text)

if __name__=="__main__":unittest.main()
