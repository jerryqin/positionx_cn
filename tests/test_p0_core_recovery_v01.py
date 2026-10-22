from __future__ import annotations
from contextlib import redirect_stdout, ExitStack
from datetime import datetime, timedelta
import copy
import importlib.util
import io
import json
import math
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import duckdb
import pandas as pd
from research.research_store import ResearchStore
from research.forward_ret120 import Ret120ForwardPolicy
from research.strategy_portfolio import DEEP_ID, RET120_ID
from scripts import p0_core_recovery_v01 as rec
from scripts import run_ret120_csi500_forward as ret
from scripts import run_daily_ops_orchestrator_v1 as orch

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = {key: key[0] * 64 for key in ("qualification_sha256", "design_sha256", "semantics_sha256")}
NOW = datetime(2026, 10, 18, 12)
START = "20261019"


def probe(day=START):
    full = 5227
    tiers = {k: {"total": n or full-1800, "valid_bucket": n or full-1800,
                 "need": max(50, round((n or full-1800)*.8)), "pass": True}
             for k,n in rec.TIERS.items()}
    return {"returncode": 0, "stdout": "arbitrary diagnostic", "parsed_json": {
        "status": "RET120_UNIVERSE_PRIOR_FORWARD_V1_0_1_PROBE", "signal_date": day,
        "full_market_count": full, "csi1800_union": 1800, "tail_count": full-1800,
        "tier_coverage": tiers, "mutation": False, "qmt_download_called": False}}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.deep = self.root / "old.duckdb"
        self.p = {
            "version": rec.VERSION, "enabled": True, "start_date": START,
            "segment_root": str(self.root/"new"),
            "segment_db": str(self.root/"new"/"ret120.duckdb"),
            "output_dir": str(self.root/"new"/"portfolio"),
            "deep_db": str(self.deep), "pit_root": str(self.root/"pit"/"manifests"),
            "calendar": str(ROOT/"config"/"ops"/"cn_exchange_calendar_2026.json")
        }
        # Entirely synthetic legacy fixture: no production database is copied/read.
        with ResearchStore(self.deep) as s:
            ret.ensure_ret120_tables(s)
            s.save_state(RET120_ID, "fake_old_policy", {"last_processed_date":"20261013"})
            for i in range(23):
                s.con.execute("INSERT INTO forward_positions VALUES (?,?,?,?,?,?,?,?,?,?)",
                              [RET120_ID,"BASE20",f"OLD{i}","20261012","20261013",10.,1.,10.,.01,10.])
            for i in range(79):
                s.con.execute("INSERT INTO ret120_forward_signals(strategy_id,symbol,signal_date,ret120,quintile,status) VALUES (?,?,?,?,?,?)",
                              [RET120_ID,f"PENDING{i}","20261013",.2,"Q5","PENDING_ENTRY"])
            s.con.execute("CREATE TABLE old_portfolio(trade_date VARCHAR PRIMARY KEY,equity DOUBLE)")
            for date in ("20261008","20261009","20261012","20261013"):
                s.con.execute("INSERT INTO old_portfolio VALUES (?,?)",[date,999324.95])
        self.old_hash = rec.sha(self.deep)

    def initialize(self):
        return rec.initialize_segment(self.p, ret.POLICY, ARTIFACTS, authorized=True, now=NOW)

    def write_pit(self, day, time="08:20:00"):
        root = Path(self.p["pit_root"])
        root.mkdir(parents=True,exist_ok=True)
        path = root.parent / (day+".parquet")
        stamp=f"{day[:4]}-{day[4:6]}-{day[6:]}T{time}+08:00"
        rows=[]
        offset=0
        for idx,n in (("000300.SH",300),("000905.SH",500),("000852.SH",1000)):
            for i in range(n):
                rows.append({"index_code":idx,"symbol":f"{offset+i:06}.SH","snapshot_date":day,
                             "observed_at":stamp,"source":"QMT_CURRENT"})
            offset+=n
        pd.DataFrame(rows).to_parquet(path,index=False)
        m={"phase":"preopen","status":"SUCCESS","observed_at":stamp,"finished_at":stamp,
           "items":[{"dataset":"index_membership_"+idx,"file_path":str(path),"sha256":rec.sha(path),
                     "rows":n,"health":"FRESH","source_max_date":day}
                    for idx,n in (("000300.SH",300),("000905.SH",500),("000852.SH",1000))]}
        mf=root/(day+"T082000+0800_preopen.json")
        mf.write_text(json.dumps(m))
        return mf,path

    def seed_day(self, day=START, deep_equity=800000., ret_equity=1000000.):
        self.write_pit(day)
        with ResearchStore(self.deep) as s:
            s.record_session(DEEP_ID,day,500,"FAKE_OBSERVED_DEEP","000905.SH",5000.)
            s.save_state(DEEP_ID,"fake_deep",{"last_processed_date":day})
            s.con.execute("UPDATE forward_sessions SET processed_at=? WHERE strategy_id=? AND trade_date=?", [datetime.strptime(day,"%Y%m%d").replace(hour=16),DEEP_ID,day])
            s.con.execute("INSERT INTO forward_equity VALUES (?,?,?,?,?,?,?,?)",
                          [DEEP_ID,"BASE20",day,deep_equity,deep_equity,0.,0.,0])
        with ResearchStore(self.p["segment_db"]) as s:
            ret.ensure_ret120_tables(s)
            snap=pd.DataFrame([{"signal_date":day,"symbol":f"{600000+i}.SH","ret120":i/1000,
                 "rank_first":float(i+1),"quintile":"Q5" if i>=400 else "Q1",
                 "selected_q5":i>=400,"feature_valid":True,"feature_reason":"OK"} for i in range(500)])
            ret.persist_snapshot(s,snap,"FAKE_OBSERVED_RET")
            s.con.execute("UPDATE ret120_forward_snapshot_manifest SET observed_at=? WHERE signal_date=?", [datetime.strptime(day,"%Y%m%d").replace(hour=16),day])
            s.record_session(RET120_ID,day,500,"FAKE_OBSERVED_RET","000905.SH",5000.)
            s.con.execute("UPDATE forward_sessions SET processed_at=? WHERE strategy_id=? AND trade_date=?", [datetime.strptime(day,"%Y%m%d").replace(hour=16),RET120_ID,day])
            state=s.load_state(RET120_ID)
            state["last_processed_date"]=day
            s.save_state(RET120_ID,ret.POLICY.fingerprint(ARTIFACTS),state)
            s.con.execute("INSERT INTO forward_equity VALUES (?,?,?,?,?,?,?,?)",
                          [RET120_ID,"BASE20",day,ret_equity,ret_equity,0.,0.,0])

    def snapshot(self,day=START):
        clock=datetime.strptime(day+" 16:30","%Y%m%d %H:%M")
        return rec.portfolio_snapshot(self.p,day,now=clock)

    def test_defaults_deny_initialization_and_snapshot(self):
        with self.assertRaisesRegex(ValueError,"INITIALIZATION_NOT_AUTHORIZED"):
            rec.initialize_segment(self.p,ret.POLICY,ARTIFACTS,now=NOW)
        p={**self.p,"enabled":False}
        with self.assertRaisesRegex(ValueError,"RECOVERY_DISABLED"):
            rec.portfolio_snapshot(p,START)
        self.assertFalse(Path(self.p["segment_root"]).exists())

    def test_future_start_only_and_trading_day(self):
        for date in ("20261016","20261018"):
            with self.subTest(date=date),self.assertRaises(ValueError):
                rec.initialize_segment({**self.p,"start_date":date},ret.POLICY,ARTIFACTS,authorized=True,now=NOW)
        self.assertFalse(Path(self.p["segment_db"]).exists())

    def test_init_has_cursor_but_no_sessions_positions_signals_or_old_return(self):
        result=self.initialize()
        self.assertEqual(result["boundary_cursor"],"20261016")
        self.assertEqual(result["session_rows_created"],0)
        with rec.read_connection(self.p["segment_db"]) as c:
            state=json.loads(c.execute("SELECT state_json FROM forward_state").fetchone()[0])
            self.assertEqual(state["last_processed_date"],"20261016")
            self.assertIsNone(state["started_at"])
            self.assertEqual(state["base_equity"],1000000.)
            for table in ("forward_sessions","forward_equity","forward_positions","forward_signals"):
                self.assertEqual(c.execute("SELECT count(*) FROM "+table).fetchone()[0],0)
            self.assertEqual(state["pending_signals"],0)
        self.assertEqual(rec.sha(self.deep),self.old_hash)

    def test_existing_database_never_reinitialized(self):
        self.initialize()
        before=rec.sha(self.p["segment_db"])
        with self.assertRaisesRegex(ValueError,"EXISTING_DATABASE_FORBIDDEN"):
            self.initialize()
        self.assertEqual(rec.sha(self.p["segment_db"]),before)

    def test_profile_cannot_alias_legacy_database(self):
        with self.assertRaisesRegex(ValueError,"SHARED_DATABASE_FORBIDDEN"):
            rec.profile_checked({**self.p,"segment_root":str(self.root),"segment_db":str(self.deep)})

    def test_actual_probe_contract_passes(self):
        self.assertTrue(orch.positive_probe(probe(),START))
        self.assertTrue(orch.positive_probe(probe("2026-10-19"),START))

    def test_probe_rejects_dates_rc_truthiness_and_loose_pass(self):
        for change in (
            lambda r:r.update(returncode=1),
            lambda r:r.update(returncode=False),
            lambda r:r["parsed_json"].update(signal_date="20261016"),
            lambda r:r["parsed_json"].update(status="PASS_ANYTHING"),
            lambda r:r.update(parsed_json=None),
            lambda r:r["parsed_json"].update(tier_coverage={}),
            lambda r:r["parsed_json"].update(mutation=True),
        ):
            r=probe();change(r)
            with self.subTest(result=r):
                self.assertFalse(orch.positive_probe(r,START))

    def test_probe_all_four_tiers_required_and_thresholds_unchanged(self):
        for tier in rec.TIERS:
            for change in ("missing","false","string","below","low_need"):
                r=probe()
                v=r["parsed_json"]["tier_coverage"][tier]
                if change=="missing":del r["parsed_json"]["tier_coverage"][tier]
                elif change=="false":v["pass"]=False
                elif change=="string":v["pass"]="true"
                elif change=="below":v["valid_bucket"]=v["need"]-1
                else:v["need"]=1
                with self.subTest(tier=tier,change=change):
                    self.assertFalse(orch.positive_probe(r,START))

    def test_pit_hash_mismatch_and_late_capture_rejected(self):
        self.initialize()
        for time in ("09:20:00","09:21:26"):
            self.write_pit(START,time)
            with self.assertRaisesRegex(ValueError,"PIT_TIME"):
                rec.before_run(self.p,START,now=datetime(2026,10,19,16))
        mf,path=self.write_pit(START)
        m=json.loads(mf.read_text());m["items"][0]["sha256"]="0"*64;mf.write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError,"PIT_BINDING"):
            rec.before_run(self.p,START,now=datetime(2026,10,19,16))

    def test_missing_session_prevents_portfolio_write(self):
        self.initialize();self.write_pit(START)
        with self.assertRaisesRegex(ValueError,"SESSION_OR_SOURCE_MISSING"):
            self.snapshot()
        with rec.read_connection(self.p["segment_db"]) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='p0_portfolio_equity'").fetchone()[0],0)

    def test_nontrading_old_dates_and_missing_pit_fail(self):
        self.initialize()
        for day,clock in (("20261018",datetime(2026,10,18,16)),("20261019",datetime(2026,10,20,16))):
            with self.subTest(day=day),self.assertRaisesRegex(ValueError,"NOT_CURRENT_TRADING_SESSION"):
                rec.before_run(self.p,day,now=clock)
        with self.assertRaisesRegex(ValueError,"PIT_MISSING"):
            rec.before_run(self.p,START,now=datetime(2026,10,19,16))

    def test_portfolio_new_baseline_then_return_not_old_equity(self):
        self.initialize();self.seed_day()
        first=self.snapshot()
        self.assertEqual(first["portfolio_equity"],1000000.)
        self.assertTrue(first["new_segment_not_old_portfolio_return"])
        self.seed_day("20261020",deep_equity=880000.,ret_equity=1100000.)
        second=self.snapshot("20261020")
        self.assertAlmostEqual(second["portfolio_nav"],1.1)
        with rec.read_connection(self.p["segment_db"]) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM p0_portfolio_equity").fetchone()[0],2)
        with rec.read_connection(self.deep) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM old_portfolio").fetchone()[0],4)
            self.assertEqual(c.execute("SELECT count(*) FROM ret120_forward_signals WHERE status='PENDING_ENTRY'").fetchone()[0],79)
            self.assertEqual(c.execute("SELECT count(*) FROM forward_positions WHERE strategy_id=?",[RET120_ID]).fetchone()[0],23)

    def test_same_day_is_idempotent_including_public_output(self):
        self.initialize();self.seed_day();first=self.snapshot()
        p=Path(self.p["output_dir"])/"latest.json";hash1=rec.sha(p)
        self.assertEqual(self.snapshot(),first);self.assertEqual(rec.sha(p),hash1)
        with rec.read_connection(self.p["segment_db"]) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM p0_portfolio_equity").fetchone()[0],1)

    def test_same_day_changed_result_rejected_preserving_old_row(self):
        self.initialize();self.seed_day();self.snapshot()
        with duckdb.connect(str(self.deep)) as c:
            c.execute("UPDATE forward_equity SET equity=810000,cash=810000 WHERE strategy_id=?",[DEEP_ID])
        with self.assertRaisesRegex(ValueError,"BASELINE_CONFLICT"):
            self.snapshot()
        with rec.read_connection(self.p["segment_db"]) as c:
            self.assertEqual(c.execute("SELECT equity FROM p0_portfolio_equity").fetchone()[0],1000000.)

    def test_portfolio_missing_intermediate_day_is_not_backfilled(self):
        self.initialize();self.seed_day();self.snapshot();self.seed_day("20261021")
        with self.assertRaisesRegex(ValueError,"PORTFOLIO_DATE_GAP"):
            self.snapshot("20261021")
        with rec.read_connection(self.p["segment_db"]) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM p0_portfolio_equity").fetchone()[0],1)

    def test_invalid_nav_or_stale_state_is_rejected(self):
        self.initialize();self.seed_day()
        for value in (float("nan"),float("inf"),-1.,0.):
            with duckdb.connect(self.p["segment_db"]) as c:
                c.execute("UPDATE forward_equity SET equity=?",[value])
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,"INVALID_EQUITY"):
                self.snapshot()

    def test_stale_state_and_bad_source_are_not_success(self):
        self.initialize();self.seed_day()
        with duckdb.connect(self.p["segment_db"]) as c:
            c.execute("UPDATE forward_state SET state_json=?",[json.dumps({"last_processed_date":"20261016"})])
        with self.assertRaisesRegex(ValueError,"STALE_STRATEGY_STATE"):
            self.snapshot()
        with duckdb.connect(self.p["segment_db"]) as c:
            c.execute("UPDATE forward_state SET state_json=?",[json.dumps({"last_processed_date":START})])
            c.execute("UPDATE forward_sessions SET universe_source=''")
        with self.assertRaisesRegex(ValueError,"SESSION_OR_SOURCE_MISSING"):
            self.snapshot()

    def test_snapshot_digest_source_conflict_is_rejected(self):
        self.initialize();self.seed_day()
        with duckdb.connect(self.p["segment_db"]) as c:
            c.execute("UPDATE ret120_forward_snapshot_manifest SET snapshot_sha256='bad'")
        with self.assertRaisesRegex(ValueError,"RET120_SNAPSHOT_SOURCE"):
            self.snapshot()

    def test_result_contract_does_not_trust_exit_zero_or_stale_json(self):
        for payload in (None,{},{"status":"FAIL","ok":True},{"status":"PASS_P0_PORTFOLIO_SNAPSHOT","ok":True,"business_date":"20261016"}):
            self.assertIsNotNone(rec.task_result_error(payload,START,"PASS_P0_PORTFOLIO_SNAPSHOT"))

    def test_simulated_task_failure_stops_before_success(self):
        profile_file=self.root/"profile.json";profile_file.write_text(json.dumps(self.p))
        task={"recovery_profile":str(profile_file),"steps":[["fake"]]*3}
        ok={"status":"PASS_P0_PORTFOLIO_CHECK","ok":True,"business_date":START}
        bad={"status":"FAIL","ok":False,"business_date":START}
        results=[{"returncode":0,"parsed_json":p,"stdout":"","log_path":None} for p in (ok,bad)]
        with patch.object(orch,"run_step",side_effect=results) as run:
            status,rows=orch.portfolio_recovery({},task,{"project_root":str(ROOT),"date":START})
        self.assertEqual(status,"FAILED")
        self.assertEqual(run.call_count,2)
        self.assertEqual(rows[-1]["error_summary"],"P0_BUSINESS_RESULT_INVALID")

    def test_disabled_ret120_never_runs_probe_or_sdk(self):
        profile=self.root/"disabled.json";profile.write_text(json.dumps({**self.p,"enabled":False}))
        with patch.object(orch,"run_step",side_effect=AssertionError("no task calls")):
            with self.assertRaisesRegex(ValueError,"RECOVERY_DISABLED"):
                orch.ret120_forward({},{"recovery_profile":str(profile)},{"project_root":str(ROOT)})

    def test_original_runner_two_sessions_with_only_fake_sdk(self):
        self.initialize()
        symbols=[f"{600000+i}.SH" for i in range(500)]
        all_dates=pd.bdate_range(end="2026-10-20",periods=140).strftime("%Y%m%d").tolist()
        histories={}
        for i,symbol in enumerate(symbols+["000905.SH"]):
            vals=[10.+j*(i+1)*.00001 for j in range(len(all_dates))]
            histories[symbol]=pd.DataFrame({"open":vals,"high":[x+.01 for x in vals],
                "low":[x-.01 for x in vals],"close":vals,"volume":100000.,"amount":1000000.},index=all_dates)
        fake=types.ModuleType("xtquant")
        fake.xtdata=types.SimpleNamespace(get_instrument_detail=lambda *a: {
            "InstrumentName":"FAKE","InstrumentStatus":0,"OpenDate":"20000101","UpStopPrice":100.,"PriceTick":.01})
        for day in (START,"20261020"):
            self.write_pit(day)
            clock=datetime.strptime(day+" 16:30","%Y%m%d %H:%M")
            rec.before_run(self.p,day,now=clock)
            class FakeClock(datetime):
                @classmethod
                def now(cls,tz=None):return clock
            with ExitStack() as stack:
                stack.enter_context(patch.dict(sys.modules,{"xtquant":fake}))
                stack.enter_context(patch.object(ret,"datetime",FakeClock))
                stack.enter_context(patch.object(ret,"verify_frozen_prerequisites",return_value=ARTIFACTS))
                stack.enter_context(patch.object(ret,"qmt_sessions",side_effect=lambda sdk,start,end:[d for d in all_dates if start<=d<=end]))
                stack.enter_context(patch.object(ret,"current_universe",return_value={"ok":True,"members":symbols,"source":"FAKE_SECTOR_WEIGHT_EXACT"}))
                stack.enter_context(patch.object(ret,"refresh_history",return_value=None))
                stack.enter_context(patch.object(ret,"load_histories",return_value={s:h.loc[h.index<=day] for s,h in histories.items()}))
                stack.enter_context(redirect_stdout(io.StringIO()))
                self.assertEqual(ret.run_forward(Path(self.p["segment_db"]),Path(self.p["output_dir"])/"ret120"),0)
        with rec.read_connection(self.p["segment_db"]) as c:
            self.assertEqual(c.execute("SELECT trade_date FROM forward_sessions ORDER BY trade_date").fetchall(),[(START,),("20261020",)])
            self.assertEqual(c.execute("SELECT count(*) FROM forward_positions WHERE ledger='BASE20'").fetchone()[0],30)
            self.assertEqual(c.execute("SELECT count(*) FROM ret120_forward_signals WHERE signal_date='20261013'").fetchone()[0],0)
        self.assertEqual(rec.sha(self.deep),self.old_hash)

    def test_enabled_but_uninitialized_never_creates_database(self):
        with self.assertRaisesRegex(ValueError,"DATABASE_MISSING"):
            rec.segment_status(self.p)
        profile=self.root/"enabled.json";profile.write_text(json.dumps(self.p))
        with patch.object(orch,"run_step",side_effect=AssertionError("no calls before initialization")):
            with self.assertRaisesRegex(ValueError,"DATABASE_MISSING"):
                orch.ret120_forward({},{"recovery_profile":str(profile)},{"project_root":str(ROOT)})
        self.assertFalse(Path(self.p["segment_db"]).exists())

    def test_current_status_rechecks_source_after_snapshot(self):
        self.initialize();self.seed_day();self.snapshot()
        with duckdb.connect(str(self.deep)) as c:
            c.execute("UPDATE forward_sessions SET universe_source='CHANGED'")
        with self.assertRaisesRegex(ValueError,"PORTFOLIO_CURRENT_SOURCE_CONFLICT"):
            rec.portfolio_status(self.p,START,now=datetime(2026,10,19,16))

    def test_ret120_status_not_promoted_by_stdout_or_exit_zero(self):
        good={"status":"OK","health":{"status":"COLLECTING","last_processed_date":START,
              "base_equity":1000000.,"shadow_equity":1000000.},"current_verified":True}
        result={"returncode":0,"parsed_json":good,"stdout":"last_processed_date "+START}
        self.assertTrue(rec.ret120_status_current(result,START,recovery=True))
        for field,value in (("status","FAIL"),("current_verified",False)):
            bad=copy.deepcopy(result);bad["parsed_json"][field]=value
            self.assertFalse(rec.ret120_status_current(bad,START,recovery=True))

    def test_wrong_observation_date_fails_even_with_current_session_date(self):
        self.initialize();self.seed_day()
        with duckdb.connect(self.p["segment_db"]) as c:
            c.execute("UPDATE forward_sessions SET processed_at='2026-10-20 16:00:00'")
        with self.assertRaisesRegex(ValueError,"SESSION_OBSERVATION_DATE"):
            self.snapshot()


    def test_future_segment_never_reaches_probe_before_start(self):
        profile={**self.p,"start_date":"20261020"}
        rec.initialize_segment(profile,ret.POLICY,ARTIFACTS,authorized=True,now=NOW)
        file=self.root/"future.json";file.write_text(json.dumps(profile))
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return datetime(2026,10,19,16)
        with patch.object(rec,"datetime",Clock),patch.object(orch,"run_step",side_effect=AssertionError("no SDK calls before start")):
            with self.assertRaisesRegex(ValueError,"NOT_CURRENT_TRADING_SESSION"):
                orch.ret120_forward({},{"recovery_profile":str(file)},{"project_root":str(ROOT),"date":START})

    def test_original_guard_lines_remain_present(self):
        text=(ROOT/"scripts/run_ret120_csi500_forward.py").read_text()
        self.assertIn('if len(new_sessions) != 1:',text)
        self.assertIn('FAIL_CLOSED MISSED_SESSION',text)
        self.assertIn('FAIL_CLOSED RETRO_SNAPSHOT_FORBIDDEN',text)


if __name__=="__main__":
    unittest.main()
