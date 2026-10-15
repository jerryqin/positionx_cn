from __future__ import annotations
import json, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class DailyOpsCutoverContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg=json.loads((ROOT/'config/ops/daily_ops_task_registry_v1.json').read_text(encoding='utf-8'))
        cls.inst=(ROOT/'scripts/install_daily_ops_scheduler_v1.ps1').read_text(encoding='utf-8')
    def test_real_submit_and_auto_approval_closed(self):
        s=self.reg['safety_invariants'];self.assertFalse(s['real_order_submit_allowed']);self.assertFalse(s['automatic_approval_allowed']);self.assertFalse(s['live_enabled']);self.assertFalse(s['broker_write'])
    def test_manual_fire_scripts_are_forbidden(self):
        text=' '.join(self.reg['forbidden_automation']['script_names']).lower();self.assertIn('run_ret120_manual_fire_shadow_v01.py',text);self.assertIn('run_g4_manual_approval_v05.py',text)
    def test_pit_preopen_has_hard_deadline(self):
        t=self.reg['tasks']['PIT_PREOPEN'];self.assertEqual(t['calendar_scope'],'TRADING_DAY');self.assertEqual(t['deadline'],'09:20');self.assertTrue(t['exactly_once'])
    def test_pit_postclose_stops_after_success(self):self.assertTrue(self.reg['tasks']['PIT_POSTCLOSE']['exactly_once'])
    def test_deep_depends_on_a_share_daily(self):self.assertEqual(self.reg['tasks']['DEEP_FORWARD']['dependencies'],['A_SHARE_DAILY_UPDATE'])
    def test_ret120_manual_execution_not_automated(self):
        blob=json.dumps(self.reg['tasks']['RET120_FORWARD'],ensure_ascii=False).lower();self.assertNotIn('record-human',blob);self.assertNotIn('record-trade',blob);self.assertNotIn('approve',blob)
    def test_existing_optimus_kept(self):self.assertIn('PositionX Optimus Current22 0830',self.reg['scheduler']['keep_unchanged'])
    def test_deep_retry_schedule(self):self.assertEqual(self.reg['scheduler']['update_existing']['PositionX-DeepOversold-CSI500-Forward']['weekday_times'],['15:45','16:00','16:15','16:30'])
    def test_postclose_tail_retries(self):
        rows={x['name']:x for x in self.reg['scheduler']['new_tasks']};self.assertEqual(rows['PositionX-DailyOps-PostcloseTail']['times'],['16:20','16:40','17:00'])
    def test_three_watchdog_scopes_are_separate(self):
        rows={x['name']:x for x in self.reg['scheduler']['new_tasks']};self.assertEqual(rows['PositionX-DailyOps-Watchdog-Morning']['command'],'task WATCHDOG_MORNING');self.assertEqual(rows['PositionX-DailyOps-Watchdog-Postclose']['command'],'task WATCHDOG_POSTCLOSE');self.assertEqual(rows['PositionX-DailyOps-Watchdog-EOD']['command'],'task WATCHDOG_EOD')
    def test_report_and_watchdogs_have_registry_tasks(self):
        for x in ('DAILY_REPORT','WATCHDOG_MORNING','WATCHDOG_POSTCLOSE','WATCHDOG_EOD'):self.assertIn(x,self.reg['tasks'])
    def test_shadow_ops_is_five_trading_days(self):self.assertEqual(self.reg['shadow_ops_acceptance_trading_days'],5)
    def test_no_catchup_setting(self):self.assertNotIn('-StartWhenAvailable',self.inst)
    def test_exact_rollback_removes_new_tasks(self):self.assertIn('task_prestate.tsv',self.inst);self.assertIn('Unregister-ScheduledTask',self.inst);self.assertIn('Register-ScheduledTask',self.inst)
    def test_apply_never_starts_task(self):self.assertNotIn('Start-ScheduledTask',self.inst)
if __name__=='__main__':unittest.main()
