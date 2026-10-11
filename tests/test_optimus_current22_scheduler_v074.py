from __future__ import annotations
import importlib.util, sys, tempfile, unittest
from datetime import date
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"scripts/run_optimus_current22_scheduled_v074.py"

spec=importlib.util.spec_from_file_location("scheduler_v074",SCRIPT)
m=importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name]=m
spec.loader.exec_module(m)

class SchedulerTests(unittest.TestCase):
    def test_normal_daily_overlap_is_two_days(self):
        days,meta=m.compute_days(date(2026,10,12),date(2026,10,11),2,30)
        self.assertEqual(days,2)

    def test_weekend_or_offline_gap_is_caught_up(self):
        days,meta=m.compute_days(date(2026,10,12),date(2026,10,9),2,30)
        self.assertEqual(days,4)

    def test_same_day_retry_keeps_two_day_overlap(self):
        days,meta=m.compute_days(date(2026,10,10),date(2026,10,10),2,30)
        self.assertEqual(days,2)

    def test_long_gap_over_limit_fails_closed(self):
        with self.assertRaises(RuntimeError):
            m.compute_days(date(2026,12,1),date(2026,10,1),2,30)

    def test_no_prior_status_uses_min_overlap(self):
        days,meta=m.compute_days(date(2026,10,10),None,2,30)
        self.assertEqual(days,2)

    def test_extract_json_accepts_progress_prefix(self):
        x=m.extract_json('[CNINFO] date 1/2\n{"status":"PASS","proposal_count":0}\n')
        self.assertEqual(x["status"],"PASS")


    def test_attention_persists_until_acknowledged(self):
        old=m.DATA_ROOT
        try:
            with tempfile.TemporaryDirectory() as td:
                m.DATA_ROOT=Path(td)
                q=[{
                    "evidence_id":"E_TEST_1","symbol":"002979.SZ","company":"雷赛智能",
                    "queue_state":"EVIDENCE_PROPOSAL","evidence_level":6,
                    "tesla_link_level":0,"source_ref":"official","event_time":"2026-10-09T00:00:00+08:00"
                }]
                x=m.merge_attention(q,"2026-10-10T08:30:00+08:00")
                self.assertEqual(len(x["pending"]),1)
                # Empty later run does not clear pending human-review work.
                x=m.merge_attention([],"2026-10-11T08:30:00+08:00")
                self.assertEqual(len(x["pending"]),1)
                out=m.acknowledge_attention("E_TEST_1")
                self.assertEqual(out["pending_attention_count"],0)
        finally:
            m.DATA_ROOT=old

    def test_scheduler_has_no_mutation_calls(self):
        s=SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("candidate_registry_v03.py\",\"ingest",s)
        self.assertNotIn("candidate_registry_v03.py\",\"apply",s)
        self.assertNotIn("canonicalization",s.lower().replace('"canonicalization_mutation":false',""))

if __name__=="__main__":
    unittest.main()
