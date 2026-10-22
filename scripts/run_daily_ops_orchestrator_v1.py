from __future__ import annotations
import argparse, datetime as dt, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from core.ops_runtime import RunStore,TaskLock,command_forbidden,norm_date,qmt_is_trading_day,render_argv,run_process,sha256_file

def load_registry(project_root):
    p=Path(project_root)/"config"/"ops"/"daily_ops_task_registry_v1.json"
    return json.loads(p.read_text(encoding="utf-8")),p
def today8(): return dt.datetime.now().astimezone().strftime("%Y%m%d")
def log_path(data_root,business_date,task_id,run_id,step):
    return Path(data_root)/"ops_runtime"/"logs"/norm_date(business_date)/task_id/f"{run_id}_{step:02d}.log"
def run_step(registry,ctx,step_no,argv):
    argv=render_argv(argv,ctx["project_root"],ctx["date"]); bad=command_forbidden(argv,registry)
    if bad: raise RuntimeError(bad)
    r=run_process(argv,ctx["project_root"],log_path(ctx["data_root"],ctx["date"],ctx["task_id"],ctx["run_id"],step_no),{"PYTHONDONTWRITEBYTECODE":"1"})
    return {"argv":argv,"returncode":r["returncode"],"parsed_json":r["parsed_json"],"log_path":r["log_path"],"stdout":r["stdout"]}
BUSINESS_NON_SUCCESS_PREFIXES=("FAIL","ERROR","BLOCKED","WAITING","PARTIAL","DEGRADED","NOT_READY","INCOMPLETE","RED")
REQUIRED_TRUE_FIELDS={
    "HEALTH_PREOPEN":"ready",
    "BROKER_RECONCILE_PREOPEN":"ok",
    "BROKER_RECONCILE_PRECLOSE":"ok",
    "BROKER_RECONCILE_FINAL":"ok",
}
def business_status_ok(parsed_json):
    if not isinstance(parsed_json,dict): return True
    st=str(parsed_json.get("status","")).strip().upper()
    if not st:return True
    return not any(st.startswith(p) for p in BUSINESS_NON_SUCCESS_PREFIXES)
def business_result_error(task_id,parsed_json):
    field=REQUIRED_TRUE_FIELDS.get(task_id)
    if field is not None:
        if not isinstance(parsed_json,dict):
            return "BUSINESS_RESULT_INVALID: expected JSON object"
        if "status" in parsed_json:
            status=parsed_json["status"]
            if not isinstance(status,str) or not status.strip():
                return "BUSINESS_STATUS_INVALID: expected nonempty string"
    if not business_status_ok(parsed_json):
        return f"BUSINESS_STATUS_NON_SUCCESS: {parsed_json['status']}"
    if field is not None:
        if field not in parsed_json:
            return f"BUSINESS_RESULT_MISSING_FIELD: {field}"
        if type(parsed_json[field]) is not bool:
            return f"BUSINESS_RESULT_INVALID_FIELD: {field} must be boolean"
        if parsed_json[field] is not True:
            return f"BUSINESS_RESULT_FALSE: {field}"
    return None

def generic(registry,task,ctx):
    results=[]
    for i,step in enumerate(task.get("steps",[]),1):
        r=run_step(registry,ctx,i,step);results.append(r)
        error=(f"SUBPROCESS_EXIT_NONZERO: rc={r['returncode']}" if r["returncode"]!=0 else
               business_result_error(ctx["task_id"],r.get("parsed_json")))
        if error:
            r["error_summary"]=error
            return "FAILED",results
    return "SUCCESS",results
def a_share_status_current(parsed_json,business_date):
    if not isinstance(parsed_json,dict):return False
    d=norm_date(business_date);d10=f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    state=parsed_json.get("state") or {}
    catalog=parsed_json.get("catalog_daily") or {}
    if norm_date(state.get("last_processed_date",""))!=d:return False
    if str(catalog.get("max_date","")).strip()!=d10:return False
    sessions=state.get("sessions") or []
    return any(norm_date(x.get("trade_date",""))==d for x in sessions if isinstance(x,dict))
def a_share_daily(registry,task,ctx):
    seq=[["<python>","scripts/update_a_share_daily_v1.py","status"],
         ["<python>","scripts/update_a_share_daily_v1.py","check"],
         ["<python>","scripts/update_a_share_daily_v1.py","run","--target-date","<trade_date>"],
         ["<python>","scripts/update_a_share_daily_v1.py","status"]]
    results=[]
    for i,step in enumerate(seq,1):
        r=run_step(registry,ctx,i,step);results.append(r)
        if i==1 and r["returncode"]==0 and a_share_status_current(r.get("parsed_json"),ctx["date"]):
            return "SUCCESS",results
        if r["returncode"]!=0:return "FAILED",results
    return ("SUCCESS" if a_share_status_current(results[-1].get("parsed_json"),ctx["date"]) else "FAILED"),results
def deep_forward(registry,task,ctx):
    ps=["powershell.exe","-NoProfile","-ExecutionPolicy","Bypass","-File",str(Path(ctx["project_root"])/"scripts"/"run_deep_oversold_forward_scheduled.ps1")]
    segment=task.get("segment_profile")
    if segment:ps.extend(["-SegmentProfile",str(Path(ctx["project_root"])/segment)])
    r1=run_step(registry,ctx,1,ps);results=[r1]
    if r1["returncode"]!=0:return "FAILED",results
    verify=["<python>","scripts/verify_deep_oversold_forward_backup.py","--latest"]
    if segment:verify.extend(["--segment-profile",str(Path(ctx["project_root"])/segment)])
    r2=run_step(registry,ctx,2,verify);results.append(r2)
    if r2["returncode"]!=0:return "FAILED",results
    pj=r2["parsed_json"] or {}
    if segment and (pj.get("ok") is not True or norm_date((pj.get("state") or {}).get("last_processed_date",""))!=norm_date(ctx["date"])):return "FAILED",results
    return ("FAILED" if pj.get("ok") is False else "SUCCESS"),results
def positive_probe(r,business_date):
    from scripts.p0_core_recovery_v01 import positive_probe as strict_probe
    return strict_probe(r,business_date)
def ret120_forward(registry,task,ctx):
    # New-segment routing is disabled unless an explicit profile is enabled.
    profile_path=task.get("recovery_profile")
    if profile_path:
        from scripts.p0_core_recovery_v01 import load_profile,require_initialized,before_run
        profile_path=str(Path(ctx["project_root"])/profile_path)
        profile=load_profile(profile_path)
        require_initialized(profile)
        before_run(profile,ctx["date"])
    results=[];counter=0
    def go(step):
        nonlocal counter
        if profile_path and "scripts/run_ret120_csi500_forward.py" in step:
            step=step+["--recovery-profile",profile_path]
        counter+=1;r=run_step(registry,ctx,counter,step);results.append(r);return r
    r=go(["<python>","scripts/run_ret120_universe_prior_forward_v10.py","probe","--signal-date","<trade_date>"])
    if not positive_probe(r,ctx["date"]):
        if go(["<python>","scripts/run_ret120_universe_prior_forward_v10.py","refresh","--signal-date","<trade_date>"])["returncode"]!=0:return "FAILED",results
        r=go(["<python>","scripts/run_ret120_universe_prior_forward_v10.py","probe","--signal-date","<trade_date>"])
        if not positive_probe(r,ctx["date"]):return "FAILED",results
    cap=go(["<python>","scripts/run_ret120_universe_prior_forward_v10.py","capture","--signal-date","<trade_date>"])
    if cap["returncode"]!=0 and "already" not in cap["stdout"].lower() and "immutable" not in cap["stdout"].lower():return "FAILED",results
    from scripts.p0_core_recovery_v01 import ret120_status_current
    st=go(["<python>","scripts/run_ret120_csi500_forward.py","status"])
    if ret120_status_current(st,ctx["date"],recovery=bool(profile_path)):return "SUCCESS",results
    if st["returncode"]!=0 or not isinstance(st.get("parsed_json"),dict) or st["parsed_json"].get("status")!="OK":
        st["error_summary"]="P0_RET120_STATUS_INVALID"
        return "FAILED",results
    if go(["<python>","scripts/run_ret120_csi500_forward.py","check"])["returncode"]!=0:return "FAILED",results
    if go(["<python>","scripts/run_ret120_csi500_forward.py","run"])["returncode"]!=0:return "FAILED",results
    st=go(["<python>","scripts/run_ret120_csi500_forward.py","status"])
    if not ret120_status_current(st,ctx["date"],recovery=bool(profile_path)):
        st["error_summary"]="P0_RET120_NOT_CURRENT"
        return "FAILED",results
    return "SUCCESS",results
def portfolio_recovery(registry,task,ctx):
    from scripts.p0_core_recovery_v01 import task_result_error,load_profile
    load_profile(Path(ctx["project_root"])/task["recovery_profile"])
    statuses=("PASS_P0_PORTFOLIO_CHECK","PASS_P0_PORTFOLIO_SNAPSHOT","PASS_P0_PORTFOLIO_STATUS")
    if len(task.get("steps",[]))!=3:raise ValueError("P0_STEP_SET_INVALID")
    results=[]
    for i,(step,expected) in enumerate(zip(task["steps"],statuses),1):
        r=run_step(registry,ctx,i,step);results.append(r)
        error=("P0_SUBPROCESS_FAILED" if r["returncode"]!=0 else
               task_result_error(r.get("parsed_json"),ctx["date"],expected))
        if error:
            r["error_summary"]=error
            return "FAILED",results
    return "SUCCESS",results

HANDLERS={"PORTFOLIO_RECOVERY":portfolio_recovery,"GENERIC":generic,"A_SHARE_DAILY_UPDATE":a_share_daily,"DEEP_FORWARD":deep_forward,"RET120_FORWARD":ret120_forward,"PIT_PREOPEN":generic,"PIT_POSTCLOSE":generic}

def execute_task(task_id,project_root,data_root,business_date,scheduled_source):
    registry,registry_path=load_registry(project_root); task=registry["tasks"][task_id]
    store=RunStore(registry["ops_storage"]["run_root"]);date=norm_date(business_date);p,obj=store.new(task_id,date,scheduled_source)
    try:
        if task.get("paused") is True:return store.finish(p,obj,"PAUSED",error_summary="EXPLICITLY_AUTHORIZED_NONCORE_PAUSE")
        if task.get("calendar_scope")=="TRADING_DAY":
            try:trading=qmt_is_trading_day(date,registry["calendar"]["market"])
            except Exception as e:return store.finish(p,obj,"FAILED",error_summary=f"TRADING_CALENDAR_UNAVAILABLE: {type(e).__name__}: {e}")
            if not trading:return store.finish(p,obj,"SKIPPED_NON_TRADING_DAY",error_summary=None)
        if task.get("handler")=="PIT_PREOPEN" and task.get("deadline"):
            hh,mm=map(int,task["deadline"].split(":"));now=dt.datetime.now().astimezone()
            if now.hour>hh or (now.hour==hh and now.minute>=mm):return store.finish(p,obj,"FAILED",error_summary="PREOPEN_DEADLINE_MISSED_NO_HINDSIGHT_CAPTURE")
        if task.get("exactly_once") and store.successful(date,task_id):return store.finish(p,obj,"SKIPPED_ALREADY_SUCCESS",error_summary=None)
        deps={dep:store.successful(date,dep) for dep in task.get("dependencies",[])};obj["dependency_state"]=deps
        if deps and not all(deps.values()):return store.finish(p,obj,"BLOCKED",error_summary=f"DEPENDENCY_NOT_READY: {deps}")
        with TaskLock(registry["ops_storage"]["lock_root"],task_id,date):
            ctx={"project_root":project_root,"data_root":data_root,"date":date,"task_id":task_id,"run_id":obj["run_id"]}
            status,steps=HANDLERS[task["handler"]](registry,task,ctx); last=steps[-1] if steps else {}
            return store.finish(p,obj,status,exit_code=0 if status=="SUCCESS" else 1,
                                error_summary=last.get("error_summary"),
                                command_identity=json.dumps([x.get("argv") for x in steps],ensure_ascii=False),
                                code_or_config_sha256=sha256_file(registry_path),log_path=last.get("log_path"),
                                parsed_summary=last.get("parsed_json"),output_artifacts=[x.get("log_path") for x in steps if x.get("log_path")],
                                output_sha256=[sha256_file(x["log_path"]) for x in steps if x.get("log_path") and Path(x["log_path"]).is_file()])
    except Exception as e:return store.finish(p,obj,"FAILED",exit_code=1,error_summary=f"{type(e).__name__}: {e}")

def run_phase(name,project_root,data_root,business_date,scheduled_source):
    registry,_=load_registry(project_root);out=[execute_task(t,project_root,data_root,business_date,scheduled_source) for t in registry["phases"][name]]
    print(json.dumps({"phase":name,"business_date":business_date,"tasks":out},ensure_ascii=False,indent=2))
    return 0 if all(x["status"] in ("SUCCESS","SKIPPED_ALREADY_SUCCESS","SKIPPED_NON_TRADING_DAY") for x in out) else 2
def main():
    ap=argparse.ArgumentParser();ap.add_argument("mode",choices=["task","phase"]);ap.add_argument("name")
    ap.add_argument("--project-root",default=str(ROOT));ap.add_argument("--data-root",default=r"D:\positionx_data");ap.add_argument("--business-date");ap.add_argument("--scheduled-source",default="manual")
    a=ap.parse_args();date=a.business_date or today8()
    if a.mode=="task":
        r=execute_task(a.name,a.project_root,a.data_root,date,a.scheduled_source);print(json.dumps(r,ensure_ascii=False,indent=2));return 0 if r["status"] in ("SUCCESS","SKIPPED_ALREADY_SUCCESS","SKIPPED_NON_TRADING_DAY") else 2
    return run_phase(a.name,a.project_root,a.data_root,date,a.scheduled_source)
if __name__=="__main__":raise SystemExit(main())
