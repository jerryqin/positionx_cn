from __future__ import annotations
import argparse, datetime as dt, json, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from core.ops_runtime import RunStore,norm_date,write_json_atomic

def load_registry(project_root):
    return json.loads((Path(project_root)/"config"/"ops"/"daily_ops_task_registry_v1.json").read_text(encoding="utf-8"))
def scheduler_snapshot():
    ps="$x=@(); foreach($t in Get-ScheduledTask){ if($t.TaskName -match '^PositionX'){ $x += [pscustomobject]@{TaskName=$t.TaskName;State=[string]$t.State;Enabled=[bool]$t.Settings.Enabled} } }; @($x)|ConvertTo-Json -Depth 4 -Compress"
    cp=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",ps],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8",errors="replace",check=False)
    if cp.returncode!=0:return {"ok":False,"error":cp.stderr.strip()}
    raw=[] if not cp.stdout.strip() else json.loads(cp.stdout)
    if isinstance(raw,dict):raw=[raw]
    return {"ok":True,"tasks":raw}
def check(date,project_root,data_root,scope):
    reg=load_registry(project_root);d=norm_date(date);store=RunStore(reg["ops_storage"]["run_root"])
    groups={"morning":["HEALTH_PREOPEN","BROKER_RECONCILE_PREOPEN","PIT_PREOPEN","OPTIMUS_STATUS","OPTIMUS_ACCEPTANCE"],
            "postclose":["A_SHARE_DAILY_UPDATE","DEEP_FORWARD","RET120_FORWARD","BROKER_RECONCILE_FINAL"],
            "eod":["PIT_POSTCLOSE"]}
    rows=[{"task_id":tid,"status":(store.latest(d,tid) or {}).get("status","NOT_RUN")} for tid in groups.get(scope,[])]
    bad=[x for x in rows if x["status"] not in ("SUCCESS","SKIPPED_ALREADY_SUCCESS","SKIPPED_NON_TRADING_DAY")]
    rep=Path(reg["ops_storage"]["report_root"])/"daily"/d/"report.json"
    obj={"schema":"POSITIONX_CN_DAILY_OPS_WATCHDOG_V1","date":d,"scope":scope,"status":"RED" if bad else "GREEN",
         "task_findings":rows,"bad":bad,"daily_report_exists":rep.is_file(),"scheduler":scheduler_snapshot()}
    out=Path(reg["ops_storage"]["watchdog_root"])/d;out.mkdir(parents=True,exist_ok=True);write_json_atomic(out/f"{scope}_{dt.datetime.now().strftime('%H%M%S')}.json",obj)
    print(json.dumps(obj,ensure_ascii=False,indent=2));return 0 if not bad else 2
def main():
    ap=argparse.ArgumentParser();ap.add_argument("command",choices=["check"]);ap.add_argument("--scope",choices=["morning","postclose","eod"],required=True)
    ap.add_argument("--date");ap.add_argument("--project-root",default=str(ROOT));ap.add_argument("--data-root",default=r"D:\positionx_data")
    a=ap.parse_args();return check(a.date or dt.datetime.now().astimezone().strftime("%Y%m%d"),a.project_root,a.data_root,a.scope)
if __name__=="__main__":raise SystemExit(main())
