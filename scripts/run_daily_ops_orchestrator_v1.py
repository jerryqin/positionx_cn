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
def generic(registry,task,ctx):
    results=[]
    for i,step in enumerate(task.get("steps",[]),1):
        r=run_step(registry,ctx,i,step);results.append(r)
        if r["returncode"]!=0:return "FAILED",results
    return "SUCCESS",results
def a_share_daily(registry,task,ctx):
    seq=[["<python>","scripts/update_a_share_daily_v1.py","status"],
         ["<python>","scripts/update_a_share_daily_v1.py","check"],
         ["<python>","scripts/update_a_share_daily_v1.py","run","--target-date","<trade_date>"],
         ["<python>","scripts/update_a_share_daily_v1.py","status"]]
    results=[]
    for i,step in enumerate(seq,1):
        r=run_step(registry,ctx,i,step);results.append(r)
        if i==1 and r["returncode"]==0 and "SESSION_COMMITTED" in r["stdout"] and norm_date(ctx["date"]) in r["stdout"].replace("-",""):
            return "SUCCESS",results
        if r["returncode"]!=0:return "FAILED",results
    return ("SUCCESS" if "SESSION_COMMITTED" in results[-1]["stdout"] else "FAILED"),results
def deep_forward(registry,task,ctx):
    ps=["powershell.exe","-NoProfile","-ExecutionPolicy","Bypass","-File",str(Path(ctx["project_root"])/"scripts"/"run_deep_oversold_forward_scheduled.ps1")]
    r1=run_step(registry,ctx,1,ps);results=[r1]
    if r1["returncode"]!=0:return "FAILED",results
    r2=run_step(registry,ctx,2,["<python>","scripts/verify_deep_oversold_forward_backup.py","--latest"]);results.append(r2)
    if r2["returncode"]!=0:return "FAILED",results
    pj=r2["parsed_json"] or {}
    return ("FAILED" if pj.get("ok") is False else "SUCCESS"),results
def positive_probe(r):
    if r["returncode"]!=0:return False
    pj=r["parsed_json"] or {}; st=str(pj.get("status",""))
    if st.startswith("PASS_"):return True
    if any(pj.get(k) is True for k in ("coverage_ok","complete","ok")):return True
    return "PASS_RET120_UNIVERSE_PRIOR" in r["stdout"]
def ret120_forward(registry,task,ctx):
    results=[];counter=0
    def go(step):
        nonlocal counter
        counter+=1;r=run_step(registry,ctx,counter,step);results.append(r);return r
    r=go(["<python>","scripts/run_ret120_universe_prior_forward_v10.py","probe","--signal-date","<trade_date>"])
    if not positive_probe(r):
        if go(["<python>","scripts/run_ret120_universe_prior_forward_v10.py","refresh","--signal-date","<trade_date>"])["returncode"]!=0:return "FAILED",results
        r=go(["<python>","scripts/run_ret120_universe_prior_forward_v10.py","probe","--signal-date","<trade_date>"])
        if not positive_probe(r):return "FAILED",results
    cap=go(["<python>","scripts/run_ret120_universe_prior_forward_v10.py","capture","--signal-date","<trade_date>"])
    if cap["returncode"]!=0 and "already" not in cap["stdout"].lower() and "immutable" not in cap["stdout"].lower():return "FAILED",results
    st=go(["<python>","scripts/run_ret120_csi500_forward.py","status"])
    if st["returncode"]==0 and norm_date(ctx["date"]) in st["stdout"].replace("-","") and "last_processed_date" in st["stdout"]:return "SUCCESS",results
    if go(["<python>","scripts/run_ret120_csi500_forward.py","check"])["returncode"]!=0:return "FAILED",results
    if go(["<python>","scripts/run_ret120_csi500_forward.py","run"])["returncode"]!=0:return "FAILED",results
    st=go(["<python>","scripts/run_ret120_csi500_forward.py","status"])
    if st["returncode"]!=0 or "last_processed_date" not in st["stdout"] or norm_date(ctx["date"]) not in st["stdout"].replace("-",""):return "FAILED",results
    return "SUCCESS",results
HANDLERS={"GENERIC":generic,"A_SHARE_DAILY_UPDATE":a_share_daily,"DEEP_FORWARD":deep_forward,"RET120_FORWARD":ret120_forward,"PIT_PREOPEN":generic,"PIT_POSTCLOSE":generic}

def execute_task(task_id,project_root,data_root,business_date,scheduled_source):
    registry,registry_path=load_registry(project_root); task=registry["tasks"][task_id]
    store=RunStore(registry["ops_storage"]["run_root"]);date=norm_date(business_date);p,obj=store.new(task_id,date,scheduled_source)
    try:
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
