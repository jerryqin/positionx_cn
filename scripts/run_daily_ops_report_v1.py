from __future__ import annotations
import argparse, datetime as dt, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from core.ops_runtime import RunStore,norm_date,write_json_atomic

def load_registry(project_root):
    return json.loads((Path(project_root)/"config"/"ops"/"daily_ops_task_registry_v1.json").read_text(encoding="utf-8"))
def daily(date,project_root,data_root):
    reg=load_registry(project_root);store=RunStore(reg["ops_storage"]["run_root"]);d=norm_date(date);rows=[];red=False;yellow=False
    for tid,spec in reg["tasks"].items():
        runs=store.list_runs(d,tid);latest=runs[-1] if runs else None;status=(latest or {}).get("status","NOT_RUN")
        expected=not(spec["calendar_scope"]=="TRADING_DAY" and status=="SKIPPED_NON_TRADING_DAY")
        if status=="PAUSED":yellow=True
        elif expected and spec["criticality"]=="BLOCKING" and status not in ("SUCCESS","SKIPPED_ALREADY_SUCCESS"):red=True
        elif expected and status in ("FAILED","BLOCKED","PARTIAL","NOT_RUN","PAUSED"):yellow=True
        rows.append({"task_id":tid,"task_expected":expected,"task_started_at":(latest or {}).get("actual_started_at"),
                     "task_finished_at":(latest or {}).get("finished_at"),"task_status":status,
                     "skip_reason":(latest or {}).get("error_summary") if str(status).startswith("SKIPPED") else None,
                     "retry_count":max(0,len(runs)-1),"dependency_status":(latest or {}).get("dependency_state"),
                     "input_artifact":None,"output_artifact":(latest or {}).get("output_artifacts"),
                     "output_sha256":(latest or {}).get("output_sha256"),"log_path":(latest or {}).get("log_path"),
                     "freshness":None,"manual_attention":spec["criticality"]=="ATTENTION" and status not in ("SUCCESS","SKIPPED_ALREADY_SUCCESS")})
    overall="RED" if red else ("YELLOW" if yellow else "GREEN")
    obj={"schema":"POSITIONX_CN_DAILY_OPS_REPORT_V1","trade_date":d,"calendar_date":d,"overall_status":overall,
         "tasks":rows,"broker_reconcile_status":next((x["task_status"] for x in rows if x["task_id"]=="BROKER_RECONCILE_FINAL"),"NOT_RUN"),
         "external_activity_summary":None,"live_enabled":reg["safety_invariants"]["live_enabled"],
         "auto_execute":reg["safety_invariants"]["auto_execute"],"broker_write":reg["safety_invariants"]["broker_write"],
         "auto_live_order_count":0,"shadow_ops":{"required_trading_days":reg["shadow_ops_acceptance_trading_days"],"automatic_transition_to_m3":False}}
    out=Path(reg["ops_storage"]["report_root"])/"daily"/d;out.mkdir(parents=True,exist_ok=True);write_json_atomic(out/"report.json",obj)
    md=["# PositionX-CN Daily Ops Report",f"- Date: `{d}`",f"- Overall: **{overall}**","",
        "| Task | Expected | Status | Retries | Attention |","|---|---:|---|---:|---:|"]
    for r in rows:md.append(f"| {r['task_id']} | {str(r['task_expected'])} | {r['task_status']} | {r['retry_count']} | {str(r['manual_attention'])} |")
    md+=["",f"- live_enabled: `{obj['live_enabled']}`",f"- auto_execute: `{obj['auto_execute']}`",f"- broker_write: `{obj['broker_write']}`",f"- auto_live_order_count: `{obj['auto_live_order_count']}`"]
    (out/"report.md").write_text("\n".join(md)+"\n",encoding="utf-8");return obj
def aggregate(kind,dates,project_root):
    reg=load_registry(project_root);reports=[]
    for d in dates:
        p=Path(reg["ops_storage"]["report_root"])/"daily"/d/"report.json"
        if p.is_file():reports.append(json.loads(p.read_text(encoding="utf-8")))
    total=sum(len(x.get("tasks",[])) for x in reports);good=sum(1 for x in reports for t in x.get("tasks",[]) if t.get("task_status") in ("SUCCESS","SKIPPED_ALREADY_SUCCESS","SKIPPED_NON_TRADING_DAY"))
    failures=[t for x in reports for t in x.get("tasks",[]) if t.get("task_status") in ("FAILED","BLOCKED","PARTIAL","NOT_RUN","PAUSED")]
    return {"period_kind":kind,"period_dates":dates,"task_completion_rate":good/total if total else None,
            "late_or_missed_runs":sum(1 for t in failures if t.get("task_status")=="NOT_RUN"),"retry_rate":None,
            "failure_causes":[t.get("task_id") for t in failures],"mean_recovery_time":None,
            "pit_missing_or_partial":[t.get("task_id") for t in failures if "PIT" in t.get("task_id","")],
            "data_quality_incidents":[],"broker_mismatch_incidents":[],"deep_ret120_progress":{},
            "attention_aging":None,"human_interventions":None,"scheduler_drift":None,"backup_health":None}
def shadow_acceptance(project_root):
    reg=load_registry(project_root);root=Path(reg["ops_storage"]["report_root"])/"daily";green=[]
    if root.exists():
        for p in sorted(root.glob("*/report.json")):
            try:o=json.loads(p.read_text(encoding="utf-8"))
            except Exception:continue
            if o.get("overall_status")=="GREEN":green.append(o.get("trade_date"))
    need=reg["shadow_ops_acceptance_trading_days"]
    return {"required_trading_days":need,"observed_green_days":green[-need:],"ready_for_human_acceptance":len(green)>=need,"automatic_transition_to_m3":False}

def auto(date,project_root,data_root):
    d=norm_date(date);obj=daily(d,project_root,data_root);day=dt.datetime.strptime(d,"%Y%m%d").date();reg=load_registry(project_root);rr=Path(reg["ops_storage"]["report_root"])
    start=day-dt.timedelta(days=day.weekday());dates=[];cur=start
    while cur<=day:dates.append(cur.strftime("%Y%m%d"));cur+=dt.timedelta(days=1)
    week=day.isocalendar();wid=f"{week.year}-W{week.week:02d}";wdir=rr/"weekly"/wid;wdir.mkdir(parents=True,exist_ok=True)
    w=aggregate("week_to_date",dates,project_root);write_json_atomic(wdir/"report.json",w);(wdir/"report.md").write_text("# PositionX-CN Weekly Ops Report\n\n"+json.dumps(w,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    start=day.replace(day=1);dates=[];cur=start
    while cur<=day:dates.append(cur.strftime("%Y%m%d"));cur+=dt.timedelta(days=1)
    mid=day.strftime("%Y-%m");mdir=rr/"monthly"/mid;mdir.mkdir(parents=True,exist_ok=True)
    m=aggregate("month_to_date",dates,project_root);m.update({"availability":m.get("task_completion_rate"),"incidents":m.get("failure_causes"),"root_causes":[],"data_chain_integrity":None,"forward_sample_progress":None,"risk_gate_status":{"live_enabled":False,"auto_execute":False,"broker_write":False},"disk_growth":None,"backup_restore_check":None,"code_config_changes":None,"qmt_environment_changes":None,"automation_candidates":[]})
    write_json_atomic(mdir/"report.json",m);(mdir/"report.md").write_text("# PositionX-CN Monthly Ops Report\n\n"+json.dumps(m,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    obj["shadow_ops"]=shadow_acceptance(project_root);write_json_atomic(rr/"daily"/d/"report.json",obj)
    print(json.dumps(obj,ensure_ascii=False,indent=2));return 0
def main():
    ap=argparse.ArgumentParser();ap.add_argument("command",choices=["auto","daily"]);ap.add_argument("--date");ap.add_argument("--project-root",default=str(ROOT));ap.add_argument("--data-root",default=r"D:\positionx_data")
    a=ap.parse_args();d=a.date or dt.datetime.now().astimezone().strftime("%Y%m%d")
    if a.command=="daily":print(json.dumps(daily(d,a.project_root,a.data_root),ensure_ascii=False,indent=2));return 0
    return auto(d,a.project_root,a.data_root)
if __name__=="__main__":raise SystemExit(main())
