from __future__ import annotations
import json, re, sqlite3, sys
from pathlib import Path
from datetime import datetime

PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0,str(PROJECT_ROOT))

from research.g4_ledger_resolution import choose_base_ledger

RESEARCH_DB=PROJECT_ROOT/"data/research/positionx_research.duckdb"
PROD_DB=PROJECT_ROOT/"data/positionx.db"
SETTINGS=PROJECT_ROOT/"config/settings.yaml"
DEEP_STATE=PROJECT_ROOT/"data/research/forward_deep_oversold_csi500_v1_1/state.json"
RET_STATE=PROJECT_ROOT/"data/research/forward_ret120_csi500_v1_0/state.json"
G7=Path(r"D:\positionx_data\research\pilot_live_readiness\pilot_policy_freeze_v0_2_600k\pilot_policy_freeze_summary_v02_600k.json")
OUT=Path(r"D:\positionx_data\research\pilot_live_readiness\preopen_hardening_v0_1")

DEEP_ID="DEEP_OVERSOLD_CSI500_FORWARD_V1_1"
RET_ID="RET120_CSI500_FORWARD_V1_0"

def readj(p):
    return json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}

def parse_setting(text,key,kind="raw"):
    m=re.search(rf"(?m)^\s*{re.escape(key)}\s*:\s*([^#\r\n]+)",text)
    if not m:return None
    raw=m.group(1).strip()
    if kind=="bool":
        return raw.lower()=="true" if raw.lower() in ("true","false") else None
    if kind=="float":
        try:return float(raw)
        except Exception:return None
    return raw

def research_ledger_audit():
    import duckdb
    con=duckdb.connect(str(RESEARCH_DB),read_only=True)
    try:
        out={}
        for strategy,state_path in [(DEEP_ID,DEEP_STATE),(RET_ID,RET_STATE)]:
            state=readj(state_path)
            expected=int(state.get("active_positions") or 0)
            rows=con.execute(
                """select ledger,count(*) from forward_positions
                   where strategy_id=? group by ledger order by ledger""",[strategy]
            ).fetchall()
            counts={str(k):int(v) for k,v in rows}
            try:
                resolved=choose_base_ledger(counts,expected)
                ok=True; err=None
            except Exception as e:
                resolved=None;ok=False;err=f"{type(e).__name__}: {e}"
            out[strategy]={
                "state_active_positions":expected,
                "ledger_counts":counts,
                "resolved_base_ledger":resolved,
                "resolution_ok":ok,
                "error":err,
            }
        return out
    finally:
        con.close()

def sqlite_schema_snapshot():
    if not PROD_DB.exists():
        return {"exists":False,"path":str(PROD_DB),"tables":{}}
    uri=f"file:{PROD_DB.as_posix()}?mode=ro"
    con=sqlite3.connect(uri,uri=True)
    try:
        names=[r[0] for r in con.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
        ).fetchall()]
        tables={}
        for t in names:
            cols=[]
            for r in con.execute(f'pragma table_info("{t}")').fetchall():
                cols.append({"name":r[1],"type":r[2],"notnull":bool(r[3]),"pk":bool(r[5])})
            tables[t]=cols
        return {"exists":True,"path":str(PROD_DB),"tables":tables}
    finally:
        con.close()

def attribution_candidates(schema):
    wanted={"strategy_name","remark","intent_key","symbol","trade_date","order_id","trade_id","source","audit"}
    found={}
    for t,cols in schema.get("tables",{}).items():
        hits=sorted({c["name"] for c in cols} & wanted)
        if hits:found[t]=hits
    return found

def main():
    settings=SETTINGS.read_text(encoding="utf-8-sig") if SETTINGS.exists() else ""
    g7=readj(G7)
    ledgers=research_ledger_audit()
    prod=sqlite_schema_snapshot()
    attrs=attribution_candidates(prod)
    safety={
        "live_enabled":parse_setting(settings,"live_enabled","bool"),
        "auto_execute":parse_setting(settings,"auto_execute","bool"),
        "pilot_mode":parse_setting(settings,"pilot_mode","bool"),
        "pilot_max_order_value":parse_setting(settings,"pilot_max_order_value","float"),
        "pilot_max_orders_per_day":parse_setting(settings,"pilot_max_orders_per_day","float"),
    }
    checks={
        "g7_600k_frozen":g7.get("status")=="PASS_G7_EXECUTION_PILOT_POLICY_V0_2_600K_FREEZE",
        "deep_base_ledger_resolved":ledgers[DEEP_ID]["resolution_ok"] and ledgers[DEEP_ID]["resolved_base_ledger"] is not None,
        "ret120_zero_position_state_consistent":ledgers[RET_ID]["resolution_ok"],
        "production_db_read_only_schema_visible":prod.get("exists") is True,
        "live_disabled":safety["live_enabled"] is False,
        "auto_execute_false":safety["auto_execute"] is False,
        "pilot_mode_true":safety["pilot_mode"] is True,
        "pilot_single_order_50k":safety["pilot_max_order_value"]==50000,
        "canary_daily_quota_1":safety["pilot_max_orders_per_day"]==1,
    }
    status="PASS_PREOPEN_HARDENING_V0_1" if all(checks.values()) else "BLOCKED_PREOPEN_HARDENING_V0_1"
    result={
        "status":status,
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "official_expected_next_a_share_session":"20261008",
        "ledger_audit":ledgers,
        "production_settings":safety,
        "production_sqlite_schema":prod,
        "pilot_attribution_field_candidates":attrs,
        "two_layer_risk_model":{
            "broker_account_level":"Existing PositionX RiskGate remains the outer account-level safety ceiling and sees external holdings.",
            "pilot_subledger_level":"600k Execution Pilot separately tracks PositionX-tagged Pilot exposure: planned gross <=150k, hard gross <=180k, single order <=50k.",
            "invariant":"External holdings do not consume Pilot subledger gross, but they may still block an order at the outer account RiskGate."
        },
        "checks":checks,
        "safety":{
            "qmt":False,"broker":False,"production_db_mode":"READ_ONLY_SCHEMA_ONLY",
            "production_db_mutation":False,"research_db_mode":"READ_ONLY"
        }
    }
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/"preopen_hardening_summary_v01.json").write_text(
        json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8"
    )
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print(f"OUTPUT: {OUT}")
    return 0 if status.startswith("PASS") else 3

if __name__=="__main__":
    raise SystemExit(main())
