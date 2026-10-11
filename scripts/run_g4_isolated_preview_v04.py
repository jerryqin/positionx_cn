from __future__ import annotations
import argparse,json,shutil,sys,tempfile
from pathlib import Path
from datetime import date
from typing import Any,Dict
import yaml

PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:sys.path.insert(0,str(PROJECT_ROOT))

from core.execution import ExecutionEngine
from core.models import TradingPlanItem
from core.service import TradingService
from research.g4_isolated_preview_adapter import (
    PILOT_STRATEGY_NAME,incremental_target_weight,preview_evidence
)
from research.pilot_subledger import gross_guard

RESEARCH_DB=PROJECT_ROOT/"data/research/positionx_research.duckdb"
PROD_SETTINGS=PROJECT_ROOT/"config/settings.yaml"
PROD_DB=PROJECT_ROOT/"data/positionx.db"
OUT=Path(r"D:\positionx_data\research\pilot_live_readiness\g4_isolated_preview_v0_4")

def _json(x):return json.dumps(x,ensure_ascii=False,indent=2,default=str)

def _load_confirmed():
    import duckdb
    con=duckdb.connect(str(RESEARCH_DB),read_only=True)
    try:
        exists=bool(con.execute(
          "select 1 from information_schema.tables where table_schema='main' and table_name='g4_shadow_reservations'"
        ).fetchone())
        if not exists:return []
        rows=con.execute("""
          select shadow_decision_id,signal_date,strategy_id,symbol,stage,
                 confirmed_entry_trade_date,confirmed_shadow_notional_rmb,pilot_target_notional_rmb,
                 reservation_status
          from g4_shadow_reservations
          where reservation_status='CONFIRMED_READY_FOR_PREVIEW'
          order by confirmed_entry_trade_date,sample_rank
        """).fetchall()
        return [{
          "shadow_decision_id":r[0],"signal_date":r[1],"strategy_id":r[2],"symbol":r[3],
          "stage":r[4],"confirmed_entry_trade_date":r[5],
          "confirmed_shadow_notional_rmb":float(r[6]),"pilot_target_notional_rmb":float(r[7]),
          "reservation_status":r[8],
        } for r in rows]
    finally:con.close()

def _pilot_marked_gross_from_prod():
    """Read attributed Pilot fills only; mark open Pilot shares with latest production close.
    This is a pre-entry gross guard. The previewed new order is added separately.
    """
    import sqlite3
    if not PROD_DB.exists():return 0.0,{"reason":"NO_PROD_DB"}
    con=sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro",uri=True)
    con.row_factory=sqlite3.Row
    try:
        plans=[dict(r) for r in con.execute(
          "select trade_date,symbol,audit_json from trading_plan where strategy_name=?",
          (PILOT_STRATEGY_NAME,)).fetchall()]
        shares={}
        issues=[]
        for p in plans:
            try:a=json.loads(p["audit_json"] or "{}")
            except Exception:a={}
            if not (a.get("g4_shadow_decision_id") or a.get("shadow_decision_id")):
                continue
            intents=[dict(r) for r in con.execute(
              "select intent_key,side,broker_order_id from order_intents where trade_date=? and symbol=? order by created_at",
              (p["trade_date"],p["symbol"])).fetchall()]
            if len(intents)>1:
                issues.append(f"AMBIGUOUS_INTENT:{p['trade_date']}:{p['symbol']}")
                continue
            if not intents or intents[0]["broker_order_id"] is None:continue
            it=intents[0]
            for t in con.execute(
              "select direction,volume from broker_trades where order_id=?",(it["broker_order_id"],)).fetchall():
                side=str(t[0]).upper();vol=int(t[1])
                shares[p["symbol"]]=shares.get(p["symbol"],0)+(vol if side=="BUY" else -vol)
                if shares[p["symbol"]]<0:
                    issues.append(f"NEGATIVE_PILOT_SHARES:{p['symbol']}")
        if issues:raise RuntimeError(";".join(issues))
        gross=0.0;marks={}
        for s,sh in shares.items():
            if sh<=0:continue
            row=con.execute(
              "select trade_date,close from market_daily where symbol=? order by trade_date desc limit 1",(s,)
            ).fetchone()
            if not row:raise RuntimeError(f"MISSING_PILOT_MARK:{s}")
            px=float(row[1]);gross+=sh*px;marks[s]={"shares":sh,"trade_date":row[0],"close":px}
        return gross,marks
    finally:con.close()

def _isolated_config(temp_dir:Path,symbol:str)->Path:
    cfg=yaml.safe_load(PROD_SETTINGS.read_text(encoding="utf-8-sig")) or {}
    cfg.setdefault("app",{})["db_path"]=str(temp_dir/"g4_preview.db")
    cfg.setdefault("market",{})["symbols"]=[symbol]
    cfg.setdefault("execution",{})["live_enabled"]=False
    cfg["execution"]["auto_execute"]=False
    cfg["execution"]["pilot_mode"]=True
    cfg["execution"]["pilot_max_order_value"]=50000
    cfg["execution"]["pilot_max_orders_per_day"]=1
    out=temp_dir/"settings.g4_preview.yaml"
    out.write_text(yaml.safe_dump(cfg,allow_unicode=True,sort_keys=False),encoding="utf-8")
    return out

def _forbid(*args,**kwargs):
    raise RuntimeError("G4 ISOLATED PREVIEW GUARD: broker write action disabled")

def check():
    confirmed=_load_confirmed()
    result={
      "status":"PASS_G4_ISOLATED_PREVIEW_V0_4_CHECK",
      "confirmed_ready_count":len(confirmed),
      "confirmed":confirmed,
      "contract":{
        "production_plan_write":False,
        "production_db_write":False,
        "broker_write":False,
        "live_enabled":False,
        "auto_execute":False,
        "target_semantics":"broker current market value + Pilot incremental notional",
        "engine":"existing ExecutionEngine dry_run=True in temp SQLite",
      }
    }
    print(_json(result));return 0

def preview(decision_id:str|None):
    rows=_load_confirmed()
    if decision_id:
        rows=[r for r in rows if r["shadow_decision_id"]==decision_id]
    if not rows:
        print(_json({"status":"PASS_G4_ISOLATED_PREVIEW_V0_4_NOOP","reason":"NO_CONFIRMED_READY_ROW"}));return 0
    if len(rows)!=1:
        print(_json({"status":"BLOCKED_G4_ISOLATED_PREVIEW_V0_4","reason":"REQUIRE_EXACTLY_ONE_CONFIRMED_ROW",
                     "count":len(rows),"ids":[r["shadow_decision_id"] for r in rows]}));return 3
    r=rows[0]
    today=date.today().isoformat()
    expected=str(r["confirmed_entry_trade_date"]).replace("","") if r["confirmed_entry_trade_date"] else None
    if expected and expected.replace("-","")!=today.replace("-",""):
        print(_json({"status":"BLOCKED_G4_ISOLATED_PREVIEW_V0_4","reason":"WRONG_TRADE_DATE",
                     "confirmed_entry_trade_date":expected,"today":today}));return 3

    current_gross,gross_meta=_pilot_marked_gross_from_prod()
    gg=gross_guard(current_gross,r["pilot_target_notional_rmb"])
    if not gg["allow_new_entry"]:
        print(_json({"status":"BLOCKED_G4_ISOLATED_PREVIEW_V0_4","reason":"PILOT_SUBLEDGER_GROSS_GUARD",
                     "gross_guard":gg,"gross_meta":gross_meta}));return 3

    temp_ctx=tempfile.TemporaryDirectory()
    service=None
    try:
        temp=Path(temp_ctx.name)
        cfg=_isolated_config(temp,r["symbol"])
        service=TradingService(str(cfg))
        service.connect()

        # Hard-disable all broker write paths after connecting.
        for name in ("submit_limit","cancel_order","cancel"):
            if hasattr(service.broker,name):
                setattr(service.broker,name,_forbid)

        account=service.broker.query_account()
        positions=service.broker.query_positions()
        posmap={p.symbol:p for p in positions}
        p=posmap.get(r["symbol"])
        current_mv=float(p.market_value if p else 0.0)
        planned_target=incremental_target_weight(
          current_mv,float(account.total_asset),float(r["pilot_target_notional_rmb"])
        )

        audit={
          "g4_shadow_decision_id":r["shadow_decision_id"],
          "g4_signal_date":r["signal_date"],
          "g4_source_strategy_id":r["strategy_id"],
          "g4_stage":r["stage"],
          "g4_confirmed_shadow_notional_rmb":r["confirmed_shadow_notional_rmb"],
          "g4_pilot_target_notional_rmb":r["pilot_target_notional_rmb"],
          "g4_preview_mode":"ISOLATED_TEMP_DB",
          "g4_economic_evidence_eligible":False,
        }
        service.db.upsert_plan(TradingPlanItem(
          plan_date=today,trade_date=today,symbol=r["symbol"],
          target_weight=planned_target,
          reason=f"G4 EXECUTION PILOT {r['strategy_id']} {r['shadow_decision_id']}",
          strategy_name=PILOT_STRATEGY_NAME,audit=audit
        ))
        plan=service.db.plan_for_date(today)
        engine=ExecutionEngine(
          service.db,service.broker,service.market,service.risk,
          service.settings.section("risk"),
          service.settings.section("execution"),
          service.settings.section("market"),
        )

        preview_rows=engine.execute_plan(today,dry_run=True)
        mine=[x for x in preview_rows if x.get("g4_shadow_decision_id")==r["shadow_decision_id"]]
        if len(mine)!=1:
            result={"status":"BLOCKED_G4_ISOLATED_PREVIEW_V0_4","reason":"PREVIEW_IDENTITY_MISMATCH",
                    "preview":preview_rows,"expected_shadow_decision_id":r["shadow_decision_id"]}
            print(_json(result));return 3
        pr=mine[0]
        ev=preview_evidence(pr,r["pilot_target_notional_rmb"],r["confirmed_shadow_notional_rmb"])
        result={
          "status":"PASS_G4_ISOLATED_PREVIEW_V0_4" if ev["pass"] else "BLOCKED_G4_ISOLATED_PREVIEW_V0_4",
          "shadow":r,
          "account_context":{
            "total_asset":float(account.total_asset),
            "cash":float(account.cash),
            "symbol_current_market_value":current_mv,
            "symbol_current_weight":current_mv/float(account.total_asset) if account.total_asset>0 else None,
            "planned_incremental_pilot_notional_rmb":r["pilot_target_notional_rmb"],
            "planned_account_target_weight":planned_target,
          },
          "pilot_subledger_before":{"marked_gross_rmb":current_gross,"meta":gross_meta,"gross_guard":gg},
          "preview":pr,
          "evidence":ev,
          "manual_approval_payload":{
            "shadow_decision_id":r["shadow_decision_id"],
            "symbol":r["symbol"],
            "strategy_id":r["strategy_id"],
            "preview_intent_key":pr.get("intent"),
            "preview_side":pr.get("side"),
            "preview_volume":pr.get("volume"),
            "preview_limit_price":pr.get("price"),
            "preview_notional_rmb":pr.get("notional"),
            "status":"AWAIT_MANUAL_APPROVAL" if pr.get("status")=="ORDER_PREVIEW" and ev["pass"] else "NOT_APPROVABLE",
          },
          "safety":{
            "production_db_opened_for_write":False,
            "production_plan_written":False,
            "isolated_db":str(service.db.path),
            "broker_submit_guard":"FORBIDDEN",
            "broker_cancel_guard":"FORBIDDEN",
            "live_enabled":False,"auto_execute":False,
          }
        }
        OUT.mkdir(parents=True,exist_ok=True)
        (OUT/f"preview_{r['shadow_decision_id']}.json").write_text(_json(result),encoding="utf-8")
        print(_json(result));print(f"OUTPUT: {OUT}")
        return 0 if result["status"].startswith("PASS") else 3
    finally:
        if service is not None:
            try:service.disconnect()
            except Exception:pass
        temp_ctx.cleanup()

def main():
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=["check","preview"])
    p.add_argument("--decision-id")
    a=p.parse_args()
    try:return check() if a.command=="check" else preview(a.decision_id)
    except Exception as e:
        print(_json({"status":"FAIL_CLOSED_G4_ISOLATED_PREVIEW_V0_4","error":f"{type(e).__name__}: {e}"}));return 3

if __name__=="__main__":raise SystemExit(main())
