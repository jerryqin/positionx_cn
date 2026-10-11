from __future__ import annotations
import argparse, json, sqlite3, sys
from pathlib import Path
from datetime import datetime

PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:sys.path.insert(0,str(PROJECT_ROOT))

from research.pilot_subledger import (
    PILOT_STRATEGY_NAME, Fill, expected_remark,
    reconstruct_subledger, loss_ladder, gross_guard,
)

DB=PROJECT_ROOT/"data/positionx.db"
OUT=Path(r"D:\positionx_data\research\pilot_live_readiness\pilot_subledger_v0_1")

def ro():
    return sqlite3.connect(f"file:{DB.as_posix()}?mode=ro",uri=True)

def rows_dict(con,sql,args=()):
    con.row_factory=sqlite3.Row
    return [dict(r) for r in con.execute(sql,args).fetchall()]

def parse_audit(raw):
    if not raw:return {}
    try:return json.loads(raw)
    except Exception:return {"_parse_error":True,"_raw":raw}

def load_pilot_plans(con):
    rows=rows_dict(con,
      """select id,plan_date,trade_date,symbol,target_weight,reason,strategy_name,status,audit_json
         from trading_plan where strategy_name=? order by trade_date,symbol,id""",
      (PILOT_STRATEGY_NAME,))
    out=[]
    for r in rows:
        r["audit"]=parse_audit(r.pop("audit_json"))
        out.append(r)
    return out

def attribution(con):
    plans=load_pilot_plans(con)
    issues=[]; attributed=[]; chains=[]
    for p in plans:
        audit=p["audit"]
        shadow_id=audit.get("g4_shadow_decision_id") or audit.get("shadow_decision_id")
        if not shadow_id:
            issues.append({"type":"PILOT_PLAN_MISSING_SHADOW_DECISION_ID","plan_id":p["id"]})
            continue

        intents=rows_dict(con,
          """select intent_key,trade_date,symbol,side,volume,limit_price,reason,target_weight,current_weight,
                    status,broker_order_id,created_at,updated_at
             from order_intents where trade_date=? and symbol=? order by created_at,intent_key""",
          (p["trade_date"],p["symbol"]))
        # A Pilot plan may legitimately have no intent before preview/submit.
        if not intents:
            chains.append({"plan":p,"status":"PLAN_NO_INTENT_YET","intents":[]})
            continue

        # Fail closed: once a Pilot plan exists, all same-date/symbol intents must be uniquely explainable.
        # Current execution creates one intent per symbol/day.
        if len(intents)!=1:
            issues.append({"type":"AMBIGUOUS_INTENT_FOR_PILOT_PLAN","plan_id":p["id"],"count":len(intents)})
            continue
        it=intents[0]
        if it["broker_order_id"] is None:
            chains.append({"plan":p,"status":"INTENT_NO_BROKER_ORDER_YET","intent":it})
            continue

        orders=rows_dict(con,
          """select order_id,ts,symbol,direction,price,volume,traded_volume,status,strategy_name,remark,raw_json
             from broker_orders where order_id=?""",(it["broker_order_id"],))
        if len(orders)!=1:
            issues.append({"type":"BROKER_ORDER_LINK_FAILURE","intent_key":it["intent_key"],"count":len(orders)})
            continue
        o=orders[0]
        exp=expected_remark(it["trade_date"],it["intent_key"])
        checks={
          "symbol_match":o["symbol"]==it["symbol"],
          "side_match":str(o["direction"]).upper()==str(it["side"]).upper(),
          "remark_match":str(o["remark"] or "")==exp,
          "positionx_source":str(o["strategy_name"] or "").startswith("PositionX"),
        }
        if not all(checks.values()):
            issues.append({"type":"BROKER_ORDER_IDENTITY_MISMATCH","intent_key":it["intent_key"],
                           "order_id":o["order_id"],"checks":checks,"expected_remark":exp,"actual_remark":o["remark"]})
            continue

        trades=rows_dict(con,
          """select trade_id,ts,order_id,symbol,direction,price,volume,amount,raw_json
             from broker_trades where order_id=? order by ts,trade_id""",(o["order_id"],))
        tf=[]
        for t in trades:
            if t["symbol"]!=it["symbol"] or str(t["direction"]).upper()!=str(it["side"]).upper():
                issues.append({"type":"BROKER_TRADE_IDENTITY_MISMATCH","trade_id":t["trade_id"],"order_id":o["order_id"]})
                continue
            tf.append(Fill(
              trade_id=str(t["trade_id"]),order_id=int(t["order_id"]),
              trade_date=str(it["trade_date"]),symbol=str(t["symbol"]),
              side=str(t["direction"]),price=float(t["price"]),volume=int(t["volume"]),amount=float(t["amount"])
            ))
        attributed.extend(tf)
        chains.append({
          "plan":p,"status":"ATTRIBUTED","intent":it,
          "order":{"order_id":o["order_id"],"symbol":o["symbol"],"direction":o["direction"],
                   "strategy_name":o["strategy_name"],"remark":o["remark"],"status":o["status"],
                   "volume":o["volume"],"traded_volume":o["traded_volume"]},
          "trade_ids":[x.trade_id for x in tf],
        })
    return plans,attributed,chains,issues

def current_mark_candidates(con, symbols):
    # Production SQLite market_daily is close data; this is sufficient for pre-open
    # structural audit only. Live loss-ladder marking will later use fresh market data.
    marks={}; meta={}
    for s in symbols:
        r=con.execute(
          """select trade_date,close from market_daily where symbol=? order by trade_date desc limit 1""",(s,)
        ).fetchone()
        if r:
            marks[s]=float(r[1]);meta[s]={"source":"market_daily_latest_close","trade_date":r[0],"price":float(r[1])}
    return marks,meta

def main():
    con=ro()
    try:
        plans,fills,chains,issues=attribution(con)
        # Build marks only for symbols currently open according to the fill flow.
        provisional=reconstruct_subledger(fills,{}) if not fills else None
        symbols=sorted({f.symbol for f in fills})
        marks,mark_meta=current_mark_candidates(con,symbols)
        sub=reconstruct_subledger(fills,marks)
        ladder=loss_ladder(sub["total_pnl_pre_fee_rmb"]) if not sub["missing_marks"] else {
          "state":"UNAVAILABLE_MISSING_MARKS","allow_new_entries":False,
          "fee_accounting_status":"BROKER_FEES_NOT_IN_CURRENT_SQLITE_SCHEMA"
        }
        gross=gross_guard(sub["marked_gross_rmb"],0)
        legacy_px=con.execute(
          """select count(*) from broker_orders
             where coalesce(strategy_name,'') like 'PositionX%'"""
        ).fetchone()[0]
        status=(
          "PASS_PILOT_SUBLEDGER_ATTRIBUTION_V0_1"
          if not issues and not sub["missing_marks"] else
          "BLOCKED_PILOT_SUBLEDGER_ATTRIBUTION_V0_1"
        )
        result={
          "status":status,
          "generated_at":datetime.now().isoformat(timespec="seconds"),
          "contract":{
            "pilot_strategy_name":PILOT_STRATEGY_NAME,
            "identity_chain":"pilot trading_plan -> unique same-day/symbol order_intent -> broker_order_id -> broker_order -> broker_trades",
            "broker_order_identity":"symbol + side + exact PX|YYYYMMDD|intent[:8] remark + strategy_name starts PositionX",
            "legacy_positionx_orders_excluded_without_pilot_plan":True,
          },
          "current":{
            "pilot_plan_count":len(plans),
            "pilot_attributed_fill_count":len(fills),
            "legacy_or_other_positionx_broker_order_count":int(legacy_px),
            "chains":chains,
            "issues":issues,
          },
          "subledger":sub,
          "mark_meta":mark_meta,
          "loss_ladder":ladder,
          "gross_guard":gross,
          "fee_boundary":{
            "fees_present_as_columns_in_current_sqlite":False,
            "policy":"Do not pretend pre-fee PnL is exact actual-loss accounting. Use 20k/30k/40k early triggers as buffer; obtain broker fee statement/manual actual-loss check before treating 50k boundary as exact.",
          },
          "safety":{"production_db":"READ_ONLY","qmt":False,"broker":False,"production_db_mutation":False}
        }
        OUT.mkdir(parents=True,exist_ok=True)
        (OUT/"pilot_subledger_attribution_summary_v01.json").write_text(
          json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding="utf-8"
        )
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
        print(f"OUTPUT: {OUT}")
        return 0 if status.startswith("PASS") else 3
    finally:
        con.close()

if __name__=="__main__":
    raise SystemExit(main())
