from __future__ import annotations
import argparse, ast, csv, hashlib, inspect, json, os, re, sys
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

P1_SCRIPT = PROJECT_ROOT / "scripts/audit_ret120_frozen_signal_comparable_v02.py"
DEEP_SIM = PROJECT_ROOT / "research/deep_oversold_economic_gate.py"
DEEP_EVENTS = Path(r"D:\positionx_data\research\alpha2_incremental_value_v0_1\deep_oversold_export\deep_oversold_frozen_dedup_events.csv")
RET_EVENTS = Path(r"D:\positionx_data\research\ret120_vs_52wh_family_v0_2_frozen_signal\ret120_q5_candidate_events.csv")
RET_TRADES = Path(r"D:\positionx_data\research\ret120_vs_52wh_family_v0_2_frozen_signal\ret120_trades_20bps.csv")
RET_SUMMARY = Path(r"D:\positionx_data\research\ret120_vs_52wh_family_v0_2_frozen_signal\ret120_comparable_summary.json")
DEEP_SUMMARY = Path(r"D:\positionx_data\research\alpha2_incremental_value_v0_1\deep_oversold_export\deep_oversold_export_summary_v013.json")
CATALOG = Path(r"D:\positionx_data\catalog\market_data.duckdb")
OUT = Path(r"D:\positionx_data\research\pilot_live_readiness\g5_nearest_lot_replay_preflight_v0_1")

def sha(p: Path):
    if not p.exists(): return None
    h=hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda:f.read(8*1024*1024), b""): h.update(b)
    return h.hexdigest()

def csv_info(p: Path):
    if not p.exists(): return {"exists":False}
    with p.open("r",encoding="utf-8-sig",newline="") as f:
        r=csv.reader(f); header=next(r,[])
        n=1+sum(1 for _ in r) if header else 0
    return {"exists":True,"rows":max(0,n-1),"columns":header,"sha256":sha(p)}

def source_ast(p: Path):
    if not p.exists(): return {"exists":False}
    src=p.read_text(encoding="utf-8",errors="ignore")
    tree=ast.parse(src)
    funcs=[]
    classes=[]
    imports=[]
    constants=[]
    for node in tree.body:
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
            args=[a.arg for a in node.args.args]
            funcs.append({"name":node.name,"args":args,"lineno":node.lineno})
        elif isinstance(node,ast.ClassDef):
            classes.append({"name":node.name,"lineno":node.lineno})
        elif isinstance(node,(ast.Import,ast.ImportFrom)):
            imports.append(ast.get_source_segment(src,node) or "")
        elif isinstance(node,(ast.Assign,ast.AnnAssign)):
            seg=ast.get_source_segment(src,node) or ""
            if any(k in seg.lower() for k in ("output","catalog","q5","status","summary","event","trade")):
                constants.append(seg[:500])
    sim=[x for x in funcs if any(k in x["name"].lower() for k in ("sim","portfolio","price","load","metric","curve"))]
    return {"exists":True,"sha256":sha(p),"functions":funcs,"simulation_related":sim,"classes":classes,"imports":imports[:50],"candidate_constants":constants[:80]}

def duckdb_info(path: Path):
    if not path.exists(): return {"exists":False}
    import duckdb
    con=duckdb.connect(str(path),read_only=True)
    try:
        tables=[r[0] for r in con.execute("show tables").fetchall()]
        candidates=[t for t in tables if any(k in t.lower() for k in ("daily","market","bar","price"))]
        schemas={}
        samples={}
        for t in candidates[:20]:
            try:
                schemas[t]=[{"name":r[0],"type":r[1]} for r in con.execute(f"describe {t}").fetchall()]
                samples[t]=con.execute(f"select * from {t} limit 2").fetchdf().astype(str).to_dict("records")
            except Exception as e:
                schemas[t]={"error":str(e)}
        return {"exists":True,"tables":tables,"candidate_tables":candidates,"schemas":schemas,"samples":samples}
    finally: con.close()

def json_status(p: Path):
    if not p.exists(): return {"exists":False}
    try:
        x=json.loads(p.read_text(encoding="utf-8-sig"))
        return {"exists":True,"top_keys":list(x.keys()),"status":x.get("status"),"sha256":sha(p),
                "main_20bps":x.get("main_20bps"),"frozen":x}
    except Exception as e:
        return {"exists":True,"error":str(e),"sha256":sha(p)}

def main():
    report={
      "status":"G5_NEAREST_LOT_REPLAY_PREFLIGHT_V0_1",
      "generated_at":datetime.now().isoformat(timespec="seconds"),
      "purpose":"Discover exact frozen candidate/simulator/catalog interfaces before integer-lot economic replay. Read-only.",
      "files":{
        "p1_script":source_ast(P1_SCRIPT),
        "deep_simulator":source_ast(DEEP_SIM),
        "ret_candidate_events":csv_info(RET_EVENTS),
        "deep_candidate_events":csv_info(DEEP_EVENTS),
        "ret_trades":csv_info(RET_TRADES),
        "ret_summary":json_status(RET_SUMMARY),
        "deep_summary":json_status(DEEP_SUMMARY),
        "catalog":duckdb_info(CATALOG),
      },
      "safety":{"read_only":True,"qmt":False,"broker":False,"production_db":False}
    }
    # mechanical readiness assessment, no guessed semantics
    req = [
      report["files"]["p1_script"].get("exists"),
      report["files"]["deep_simulator"].get("exists"),
      report["files"]["ret_candidate_events"].get("exists"),
      report["files"]["deep_candidate_events"].get("exists"),
      report["files"]["ret_summary"].get("exists"),
      report["files"]["catalog"].get("exists"),
    ]
    report["preflight_ready"] = all(req)
    report["status"] = "PASS_G5_REPLAY_PREFLIGHT_V0_1" if report["preflight_ready"] else "BLOCKED_G5_REPLAY_PREFLIGHT_V0_1"
    OUT.mkdir(parents=True,exist_ok=True)
    out=OUT/"g5_replay_preflight_v01.json"
    tmp=out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    os.replace(tmp,out)
    # Print compact decision-relevant subset.
    compact={
      "status":report["status"],
      "p1_script_sha256":report["files"]["p1_script"].get("sha256"),
      "p1_simulation_related":report["files"]["p1_script"].get("simulation_related"),
      "deep_simulator_sha256":report["files"]["deep_simulator"].get("sha256"),
      "deep_simulation_related":report["files"]["deep_simulator"].get("simulation_related"),
      "ret_candidate_events":report["files"]["ret_candidate_events"],
      "deep_candidate_events":report["files"]["deep_candidate_events"],
      "ret_trades":report["files"]["ret_trades"],
      "ret_summary_status":report["files"]["ret_summary"].get("status"),
      "catalog_candidate_tables":report["files"]["catalog"].get("candidate_tables"),
      "catalog_schemas":report["files"]["catalog"].get("schemas"),
      "output":str(out),
      "safety":report["safety"],
    }
    print(json.dumps(compact,ensure_ascii=False,indent=2))
    return 0 if report["preflight_ready"] else 3

if __name__=="__main__": raise SystemExit(main())
