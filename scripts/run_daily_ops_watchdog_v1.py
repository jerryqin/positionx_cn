from __future__ import annotations
import argparse, datetime as dt, json, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from core.ops_runtime import RunStore,norm_date,qmt_is_trading_day,write_json_atomic

MORNING_TASKS=[
    "HEALTH_PREOPEN","BROKER_RECONCILE_PREOPEN","PIT_PREOPEN",
    "OPTIMUS_STATUS","OPTIMUS_ACCEPTANCE","OPTIMUS_ATTENTION",
    "G4_OPEN_CHECK","WATCHDOG_MORNING",
]
POSTCLOSE_TASKS=[
    "BROKER_RECONCILE_PRECLOSE","A_SHARE_DAILY_UPDATE","DEEP_FORWARD","RET120_FORWARD",
    "PORTFOLIO_SHADOW","CAPACITY_PROBE","G4_RESERVATION_INSPECT","PILOT_READINESS",
    "BROKER_RECONCILE_FINAL","WATCHDOG_POSTCLOSE",
]
EOD_TASKS=MORNING_TASKS+POSTCLOSE_TASKS+["PIT_POSTCLOSE","DAILY_REPORT"]
WATCHDOG_GROUPS={"morning":[x for x in MORNING_TASKS[:-1] if x!="G4_OPEN_CHECK"],"postclose":["A_SHARE_DAILY_UPDATE","DEEP_FORWARD","RET120_FORWARD","BROKER_RECONCILE_FINAL"],"eod":EOD_TASKS}

def load_registry(project_root):
    return json.loads((Path(project_root)/"config"/"ops"/"daily_ops_task_registry_v1.json").read_text(encoding="utf-8"))
def scheduler_snapshot():
    ps="$x=@(); foreach($t in Get-ScheduledTask){ if($t.TaskName -match '^PositionX'){ $x += [pscustomobject]@{TaskName=$t.TaskName;State=[string]$t.State;Enabled=[bool]$t.Settings.Enabled} } }; @($x)|ConvertTo-Json -Depth 4 -Compress"
    cp=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",ps],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8",errors="replace",check=False)
    if cp.returncode!=0:return {"ok":False,"error":cp.stderr.strip()}
    raw=[] if not cp.stdout.strip() else json.loads(cp.stdout)
    if isinstance(raw,dict):raw=[raw]
    return {"ok":True,"tasks":raw}
def classify_findings(rows,reg,trading_day=None,calendar_error=None):
    bad=[]
    if calendar_error:bad.append({"task_id":"TRADING_CALENDAR","status":"ERROR","reason":calendar_error})
    for row in rows:
        st=row["status"];tid=row["task_id"]
        if st in ("SUCCESS","SKIPPED_ALREADY_SUCCESS"):continue
        if st=="SKIPPED_NON_TRADING_DAY":
            task=reg.get("tasks",{}).get(tid,{})
            if task.get("calendar_scope")=="TRADING_DAY" and trading_day is False:continue
            reason="SKIPPED_NON_TRADING_DAY_ON_TRADING_DAY" if trading_day is True else "UNVERIFIED_NON_TRADING_DAY_SKIP"
            bad.append({**row,"reason":reason});continue
        bad.append(row)
    return bad
def apply_scope_requirements(scope,bad,daily_report_exists):
    out=list(bad)
    if scope=="eod" and not daily_report_exists:
        out.append({"task_id":"DAILY_REPORT_ARTIFACT","status":"MISSING","reason":"DAILY_REPORT_REQUIRED_FOR_EOD_GREEN"})
    return out
def check(date,project_root,data_root,scope):
    reg=load_registry(project_root);d=norm_date(date);store=RunStore(reg["ops_storage"]["run_root"])
    rows=[{"task_id":tid,"status":(store.latest(d,tid) or {}).get("status","NOT_RUN")} for tid in WATCHDOG_GROUPS.get(scope,[])]
    trading_day=None;calendar_error=None
    if any(x["status"]=="SKIPPED_NON_TRADING_DAY" for x in rows):
        try:trading_day=qmt_is_trading_day(d,reg["calendar"]["market"])
        except Exception as e:calendar_error=f"TRADING_CALENDAR_UNAVAILABLE: {type(e).__name__}: {e}"
    rep=Path(reg["ops_storage"]["report_root"])/"daily"/d/"report.json"
    daily_report_exists=rep.is_file()
    bad=classify_findings(rows,reg,trading_day,calendar_error)
    bad=apply_scope_requirements(scope,bad,daily_report_exists)
    obj={"schema":"POSITIONX_CN_DAILY_OPS_WATCHDOG_V1","date":d,"scope":scope,"status":"RED" if bad else "GREEN",
         "task_findings":rows,"bad":bad,"calendar_guard":{"checked":trading_day is not None or calendar_error is not None,"trading_day":trading_day,"error":calendar_error},
         "daily_report_exists":daily_report_exists,"scheduler":scheduler_snapshot()}
    out=Path(reg["ops_storage"]["watchdog_root"])/d;out.mkdir(parents=True,exist_ok=True);write_json_atomic(out/f"{scope}_{dt.datetime.now().strftime('%H%M%S')}.json",obj)
    print(json.dumps(obj,ensure_ascii=False,indent=2));return 0 if not bad else 2
def main():
    ap=argparse.ArgumentParser();ap.add_argument("command",choices=["check"]);ap.add_argument("--scope",choices=["morning","postclose","eod"],required=True)
    ap.add_argument("--date");ap.add_argument("--project-root",default=str(ROOT));ap.add_argument("--data-root",default=r"D:\positionx_data")
    a=ap.parse_args();return check(a.date or dt.datetime.now().astimezone().strftime("%Y%m%d"),a.project_root,a.data_root,a.scope)
if __name__=="__main__":raise SystemExit(main())
