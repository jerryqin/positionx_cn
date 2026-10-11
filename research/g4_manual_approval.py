from __future__ import annotations
import hashlib, json
from datetime import datetime, timezone

POLICY_ID="G4_MANUAL_APPROVAL_CAPSULE_V0_5"
APPROVAL_TTL_SECONDS=300
STAGE_DAILY_LIMIT={"CANARY":1,"EXPANDED":2}

def canonical_bound_fields(preview:dict, preview_sha256:str)->dict:
    if preview.get("status")!="PASS_G4_ISOLATED_PREVIEW_V0_4":
        raise ValueError("preview status is not PASS_G4_ISOLATED_PREVIEW_V0_4")
    if not bool((preview.get("evidence") or {}).get("pass")):
        raise ValueError("preview evidence.pass is false")
    p=preview.get("manual_approval_payload") or {}
    if p.get("status")!="AWAIT_MANUAL_APPROVAL":
        raise ValueError("manual approval payload is not approvable")
    if (preview.get("preview") or {}).get("status")!="ORDER_PREVIEW":
        raise ValueError("preview row is not ORDER_PREVIEW")
    s=preview.get("shadow") or {}
    safety=preview.get("safety") or {}
    if safety.get("production_plan_written") is not False:
        raise ValueError("preview safety does not prove production_plan_written=false")
    if safety.get("live_enabled") is not False or safety.get("auto_execute") is not False:
        raise ValueError("preview safety flags are not hard-disabled")

    fields={
      "policy_id":POLICY_ID,
      "preview_sha256":str(preview_sha256),
      "shadow_decision_id":str(p.get("shadow_decision_id") or ""),
      "trade_date":str(s.get("confirmed_entry_trade_date") or ""),
      "signal_date":str(s.get("signal_date") or ""),
      "strategy_id":str(p.get("strategy_id") or ""),
      "stage":str(s.get("stage") or "").upper(),
      "symbol":str(p.get("symbol") or ""),
      "intent_key":str(p.get("preview_intent_key") or ""),
      "side":str(p.get("preview_side") or ""),
      "volume":int(p.get("preview_volume") or 0),
      "limit_price":float(p.get("preview_limit_price") or 0.0),
      "preview_notional_rmb":float(p.get("preview_notional_rmb") or 0.0),
      "requested_pilot_notional_rmb":float((preview.get("evidence") or {}).get("requested_pilot_notional_rmb") or 0.0),
      "confirmed_shadow_notional_rmb":float((preview.get("evidence") or {}).get("confirmed_shadow_notional_rmb") or 0.0),
    }
    required=["shadow_decision_id","trade_date","strategy_id","stage","symbol","intent_key","side"]
    if any(not fields[k] for k in required):
        raise ValueError(f"missing required bound fields: {[k for k in required if not fields[k]]}")
    if fields["stage"] not in STAGE_DAILY_LIMIT:
        raise ValueError(f"invalid stage={fields['stage']}")
    if fields["side"] not in ("BUY","SELL"):
        raise ValueError(f"invalid side={fields['side']}")
    if fields["volume"]<=0 or fields["limit_price"]<=0 or fields["preview_notional_rmb"]<=0:
        raise ValueError("non-positive preview order fields")
    if fields["preview_notional_rmb"] > fields["requested_pilot_notional_rmb"] + 0.01:
        raise ValueError("preview exceeds requested Pilot notional")
    if fields["requested_pilot_notional_rmb"] > fields["confirmed_shadow_notional_rmb"] + 1e-9:
        raise ValueError("requested Pilot notional exceeds confirmed Shadow notional")
    if fields["requested_pilot_notional_rmb"] > 50_000 + 1e-9:
        raise ValueError("requested Pilot notional exceeds 50k")
    return fields

def approval_token(bound_fields:dict)->str:
    raw=json.dumps(bound_fields,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode("utf-8")
    return hashlib.sha256(b"POSITIONX|MANUAL_APPROVAL|V0.5|" + raw).hexdigest()

def parse_utc(ts:str)->datetime:
    x=str(ts).replace("Z","+00:00")
    dt=datetime.fromisoformat(x)
    if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def freshness(file_mtime_utc:datetime, now_utc:datetime, ttl_seconds:int=APPROVAL_TTL_SECONDS)->dict:
    age=(now_utc-file_mtime_utc).total_seconds()
    return {
      "age_seconds":age,
      "ttl_seconds":int(ttl_seconds),
      "fresh":0 <= age <= int(ttl_seconds),
    }
