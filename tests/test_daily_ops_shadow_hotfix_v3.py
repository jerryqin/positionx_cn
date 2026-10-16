from __future__ import annotations
import json, unittest
from pathlib import Path
from core.ops_runtime import local_cn_exchange_is_trading_day, qmt_is_trading_day
ROOT=Path(__file__).resolve().parents[1]
class ShadowOpsHotfixV3Tests(unittest.TestCase):
    def test_official_calendar_trading_day_20261016(self):
        self.assertTrue(local_cn_exchange_is_trading_day("20261016"))
        self.assertTrue(qmt_is_trading_day("20261016","SH"))
    def test_official_calendar_weekend_closed(self):
        self.assertFalse(local_cn_exchange_is_trading_day("20261017"))
    def test_official_calendar_national_day_closed(self):
        self.assertFalse(local_cn_exchange_is_trading_day("20261001"))
    def test_postclose_timing_has_guard_margin_and_tail_margin(self):
        reg=json.loads((ROOT/"config/ops/daily_ops_task_registry_v1.json").read_text(encoding="utf-8"))
        rows={x["name"]:x for x in reg["scheduler"]["new_tasks"]}
        self.assertEqual(rows["PositionX-DailyOps-PostcloseCore"]["times"],["15:35","15:45","15:55","16:05"])
        self.assertEqual(reg["scheduler"]["update_existing"]["PositionX-DeepOversold-CSI500-Forward"]["weekday_times"],["15:45","16:00","16:15","16:30"])
        self.assertEqual(rows["PositionX-DailyOps-PostcloseTail"]["times"],["16:20","16:40","17:00"])
        self.assertEqual(rows["PositionX-DailyOps-Watchdog-Postclose"]["times"],["17:10"])
    def test_optimus_v045_contract_remains_strict(self):
        acc=(ROOT/"config/research/optimus_v045_first_scheduled_acceptance.json").read_text(encoding="utf-8")
        self.assertIn("OPTIMUS_EVIDENCE_ACQUISITION_V0_4_5",acc)
        src=(ROOT/"scripts/run_optimus_v045_first_scheduled_acceptance.py").read_text(encoding="utf-8")
        self.assertIn("acceptance_proposals(queue)",src)
        self.assertIn("ACCEPTANCE_RAW_QUEUE_READ_FAILED",src)
if __name__=="__main__": unittest.main()
