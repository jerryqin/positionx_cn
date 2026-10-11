from __future__ import annotations
import argparse, hashlib, json, re, sys
from pathlib import Path
from datetime import datetime

PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:sys.path.insert(0,str(PROJECT_ROOT))

from research.g4_execution_equivalence import (
    ShadowOrderDecision, deterministic_sample, stable_decision_id, candidate_fingerprint
)

RESEARCH_DB=PROJECT_ROOT/"data/research/positionx_research.duckdb"
SETTINGS=PROJECT_ROOT/"config/settings.yaml"
G7=Path(r"D:\positionx_data\research\pilot_live_readiness\pilot_policy_freeze_v0_2_600k\pilot_policy_freeze_summary_v02_600k.json")
OUT=Path(r"D:\positionx_data\research\pilot_live_readiness\g4_shadow_sampling_contract_v0_1")

def read_json(p):
    return json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}

def parse_bool(text,key):
    m=re.search(rf"(?m)^\s*{re.escape(key)}\s*:\s*(true|false)\s*(?:#.*)?$",text,re.I)
    return None if not m else m.group(1).lower()=="true"

def parse_number(text,key):
    m=re.search(rf"(?m)^\s*{re.escape(key)}\s*:\s*([0-9.]+)\s*(?:#.*)?$",text)
    return None if not m else float(m.group(1))

def schema_snapshot():
    if not RESEARCH_DB.exists():
        return {"exists":False,"tables":{}}
    try:
        import duckdb
        con=duckdb.connect(str(RESEARCH_DB),read_only=True)
        wanted=["forward_signals","forward_positions","forward_trades",
                "ret120_forward_signals","ret120_forward_daily_snapshot",
                "strategy_portfolio_state","strategy_portfolio_equity"]
        out={}
        for t in wanted:
            exists=bool(con.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema='main' AND table_name=?",[t]
            ).fetchone())
            if exists:
                cols=con.execute(f"PRAGMA table_info('{t}')").fetchall()
                out[t]=[{"name":r[1],"type":r[2],"notnull":bool(r[3])} for r in cols]
            else:
                out[t]=None
        con.close()
        return {"exists":True,"tables":out}
    except Exception as e:
        return {"exists":True,"error":f"{type(e).__name__}: {e}","tables":{}}

def synthetic_sampling():
    ds=[]
    for i,(strategy,symbol,side,notional) in enumerate([
        ("DEEP_OVERSOLD_CSI500_FORWARD_V1_1","600001.SH","BUY",60000),
        ("RET120_CSI500_FORWARD_V1_0","000001.SZ","BUY",60000),
        ("DEEP_OVERSOLD_CSI500_FORWARD_V1_1","600002.SH","BUY",18000),
        ("RET120_CSI500_FORWARD_V1_0","600001.SH","BUY",60000),
    ]):
        sk=f"SYNTH-{i}"
        ds.append(ShadowOrderDecision(
            stable_decision_id("20261009",strategy,symbol,side,sk),
            "20261009",strategy,symbol,side,notional,0.01,sk
        ))
    canary=deterministic_sample(ds,"CANARY")
    expanded=deterministic_sample(ds,"EXPANDED")
    canary2=deterministic_sample(list(reversed(ds)),"CANARY")
    expanded2=deterministic_sample(list(reversed(ds)),"EXPANDED")
    checks={
      "canary_one":len(canary)==1,
      "expanded_two":len(expanded)==2,
      "order_invariant_canary":candidate_fingerprint(canary)==candidate_fingerprint(canary2),
      "order_invariant_expanded":candidate_fingerprint(expanded)==candidate_fingerprint(expanded2),
      "unique_symbols_expanded":len({r["symbol"] for r in expanded})==len(expanded),
      "never_exceeds_50k":all(r["pilot_target_notional_rmb"]<=50000 for r in canary+expanded),
      "never_enlarges_shadow":all(r["pilot_target_notional_rmb"]<=r["shadow_target_notional_rmb"] for r in canary+expanded),
    }
    return {"canary":canary,"expanded":expanded,"checks":checks}

def main():
    g7=read_json(G7)
    settings_text=SETTINGS.read_text(encoding="utf-8-sig") if SETTINGS.exists() else ""
    prod={
      "settings_exists":SETTINGS.exists(),
      "live_enabled":parse_bool(settings_text,"live_enabled"),
      "auto_execute":parse_bool(settings_text,"auto_execute"),
      "pilot_mode":parse_bool(settings_text,"pilot_mode"),
      "pilot_max_order_value":parse_number(settings_text,"pilot_max_order_value"),
      "pilot_max_orders_per_day":parse_number(settings_text,"pilot_max_orders_per_day"),
    }
    synth=synthetic_sampling()
    schema=schema_snapshot()
    checks={
      "g7_600k_frozen":g7.get("status")=="PASS_G7_EXECUTION_PILOT_POLICY_V0_2_600K_FREEZE",
      "production_live_disabled":prod["live_enabled"] is False,
      "production_auto_execute_false":prod["auto_execute"] is False,
      "pilot_mode_true":prod["pilot_mode"] is True,
      "single_order_cap_50k":prod["pilot_max_order_value"]==50000,
      "synthetic_sampling_pass":all(synth["checks"].values()),
      "research_db_readable":schema.get("exists") is True and "error" not in schema,
    }
    # The old production daily quota may still be 1. That is correct during CANARY.
    notes=[]
    if prod["pilot_max_orders_per_day"]==1:
        notes.append("PRODUCTION_DAILY_QUOTA_1_MATCHES_CANARY; DO_NOT_RAISE_TO_2_UNTIL_MANUAL_EXPANDED_PROMOTION")
    elif prod["pilot_max_orders_per_day"] is not None:
        notes.append(f"REVIEW_PRODUCTION_DAILY_QUOTA={prod['pilot_max_orders_per_day']}")
    status="PASS_G4_SHADOW_SAMPLING_CONTRACT_V0_1_PREFLIGHT" if all(checks.values()) else "BLOCKED_G4_SHADOW_SAMPLING_CONTRACT_V0_1_PREFLIGHT"
    result={
      "status":status,
      "generated_at":datetime.now().isoformat(timespec="seconds"),
      "contract":{
        "shadow_book":"12m virtual economic book / source decisions only",
        "sampling":"SHA256 deterministic; price/return/ADV/outcome blind",
        "canary":"max 1 unique-symbol sample per trade_date",
        "expanded":"max 2 unique-symbol samples per trade_date",
        "pilot_notional":"min(50k, shadow_target_notional); never enlarge shadow order",
        "manual_approval_required":True,
        "auto_execute":False,
      },
      "g7_status":g7.get("status"),
      "production_settings":prod,
      "research_schema":schema,
      "synthetic_sampling":synth,
      "checks":checks,
      "notes":notes,
      "safety":{"qmt":False,"broker":False,"production_db_opened":False,"production_db_mutation":False}
    }
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/"g4_shadow_sampling_preflight_v01.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print(f"OUTPUT: {OUT}")
    return 0 if status.startswith("PASS") else 3
if __name__=="__main__":raise SystemExit(main())
