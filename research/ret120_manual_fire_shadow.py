
from __future__ import annotations
import hashlib, json, math
from pathlib import Path
import pandas as pd

def _finite(x):
    try:
        return math.isfinite(float(x))
    except Exception:
        return False

def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""):
            h.update(b)
    return h.hexdigest()

def canonical_json(obj):
    return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str)

def recommendation_id(row, signal_date, fire_date, source_snapshot_sha256):
    payload={
        "signal_date":str(signal_date),
        "fire_date":str(fire_date),
        "symbol":str(row["symbol"]),
        "lane":str(row["lane"]),
        "lane_rank":int(row["lane_rank"]),
        "breakout_level_raw":float(row["breakout_level_raw"]),
        "stop_raw":float(row["stop_raw"]),
        "open_valid_low":float(row["open_valid_low"]),
        "open_valid_high":float(row["open_valid_high"]),
        "source_snapshot_sha256":str(source_snapshot_sha256),
    }
    return "RFS-"+hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:20]

def classify_open(actual_open, low, high, stop):
    if not _finite(actual_open):
        return "BLOCKED_MISSING_OPEN"
    a=float(actual_open); lo=float(low); hi=float(high); st=float(stop)
    if a <= st:
        return "CANCEL_INVALIDATED"
    if a < lo:
        return "CANCEL_WAIT_RECOVERY"
    if a > hi:
        return "CANCEL_WAIT_PULLBACK"
    return "FIRE_MANUAL"

def recalc_structural_size(row, actual_open, nav, sizing):
    a=float(actual_open); stop=float(row["stop_raw"])
    lot=int(sizing["board_lot"])
    if not (_finite(a) and _finite(stop) and a>stop>0 and nav>0):
        return {
            "risk_budget":0.0,"shares_by_risk":0,"shares_by_weight":0,
            "shares_by_liquidity":0,"prepare_preview_shares":int(row.get("preview_shares",0) or 0),
            "suggested_shares":0,"suggested_notional":0.0,"suggested_weight":0.0
        }
    lane=str(row["lane"])
    risk_frac=float(sizing["momentum_risk_fraction_nav"] if lane=="MOMENTUM" else sizing["mean_reversion_risk_fraction_nav"])
    max_w=float(sizing["momentum_max_weight"] if lane=="MOMENTUM" else sizing["mean_reversion_max_weight"])
    adv=float(row.get("adv20_amount",0) or 0)
    risk_budget=float(nav)*risk_frac
    risk_per_share=a-stop
    by_risk=int((risk_budget/risk_per_share)//lot*lot)
    by_weight=int(((float(nav)*max_w)/a)//lot*lot)
    by_liq=int(((adv*float(sizing["adv20_participation_max"]))/a)//lot*lot) if adv>0 else 0
    preview=int(row.get("preview_shares",0) or 0)
    candidates=[x for x in [by_risk,by_weight,by_liq] if x>=0]
    suggested=min(candidates) if candidates else 0
    if sizing.get("never_enlarge_beyond_prepare_preview_shares",True) and preview>0:
        suggested=min(suggested,preview)
    suggested=max(0,int(suggested//lot*lot))
    notional=suggested*a
    return {
        "risk_budget":risk_budget,
        "shares_by_risk":by_risk,
        "shares_by_weight":by_weight,
        "shares_by_liquidity":by_liq,
        "prepare_preview_shares":preview,
        "suggested_shares":suggested,
        "suggested_notional":notional,
        "suggested_weight":notional/float(nav) if nav>0 else 0.0,
    }

def normalize_human_fill(decision, fill_price=None, fill_shares=None, fill_fees=0.0):
    """Validate/normalize the human fill. Fees are total actual transaction costs for this fill."""
    decision=str(decision).upper()
    fp=float(fill_price) if fill_price is not None else None
    fs=int(fill_shares) if fill_shares is not None else None
    ff=float(fill_fees or 0.0)
    if ff < 0 or not _finite(ff):
        raise RuntimeError("fees must be finite and >= 0")
    if (fp is None) ^ (fs is None):
        raise RuntimeError("fill_price and fill_shares must be supplied together or both omitted")
    if fp is not None and (not _finite(fp) or fp <= 0):
        raise RuntimeError("fill_price must be finite and > 0")
    if fs is not None and fs <= 0:
        raise RuntimeError("fill_shares must be > 0")
    if decision != "APPROVE" and (fp is not None or fs is not None or ff != 0.0):
        raise RuntimeError("fill fields/fees allowed only for APPROVE")
    if fp is None and ff != 0.0:
        raise RuntimeError("fees require an actual fill")
    return fp,fs,ff

def hash_event(payload, prev_event_hash):
    obj=dict(payload)
    obj["prev_event_hash"]=prev_event_hash
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()

def append_hash_chain_jsonl(path, payload):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    prev=""
    if p.exists():
        lines=[x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        if lines:
            prev=json.loads(lines[-1]).get("event_hash","")
    event=dict(payload)
    event["prev_event_hash"]=prev
    event["event_hash"]=hash_event(payload,prev)
    with p.open("a",encoding="utf-8") as f:
        f.write(json.dumps(event,ensure_ascii=False,sort_keys=True,default=str)+"\n")
    return event
