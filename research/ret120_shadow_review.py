
from __future__ import annotations
import hashlib, json, math
from pathlib import Path
from dataclasses import dataclass
import pandas as pd
import numpy as np

def _finite(x):
    try: return math.isfinite(float(x))
    except Exception: return False

def canonical_json(obj):
    return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str)

def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()

def hash_event(payload, prev_event_hash):
    obj=dict(payload);obj["prev_event_hash"]=prev_event_hash
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()

def append_hash_chain_jsonl(path,payload):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);prev=""
    if p.exists():
        lines=[x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        if lines: prev=json.loads(lines[-1]).get("event_hash","")
    event=dict(payload);event["prev_event_hash"]=prev;event["event_hash"]=hash_event(payload,prev)
    with p.open("a",encoding="utf-8") as f: f.write(json.dumps(event,ensure_ascii=False,sort_keys=True,default=str)+"\n")
    return event

def read_jsonl(path):
    p=Path(path)
    if not p.exists(): return pd.DataFrame()
    return pd.DataFrame([json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()])

def canonicalize_gate_rows(rows):
    if rows.empty:return rows.copy()
    keep=[];compare_cols=["fire_state","actual_open","suggested_shares","suggested_notional","open_valid_low","open_valid_high","stop_raw","lane","symbol"]
    for rid,g in rows.groupby("recommendation_id",sort=False):
        vals=[tuple(str(r.get(c,"")) for c in compare_cols) for _,r in g.iterrows()]
        if len(set(vals))>1: raise RuntimeError(f"conflicting gate records for {rid}")
        keep.append(g.sort_values("_gate_run").iloc[-1])
    return pd.DataFrame(keep).reset_index(drop=True)

def first_terminal_decisions(decisions):
    cols=["recommendation_id","canonical_human_decision","canonical_decision_at","decision_conflict","human_fill_price","human_fill_shares","human_fill_fees"]
    if decisions.empty:return pd.DataFrame(columns=cols)
    x=decisions.copy();x["recorded_at"]=x["recorded_at"].astype(str);x=x.sort_values(["recommendation_id","recorded_at"]);out=[]
    for rid,g in x.groupby("recommendation_id",sort=False):
        terminals=g[g["human_decision"].isin(["APPROVE","REJECT"])]
        if terminals.empty:
            out.append({"recommendation_id":rid,"canonical_human_decision":"SKIP","canonical_decision_at":g.iloc[-1]["recorded_at"],"decision_conflict":False,"human_fill_price":np.nan,"human_fill_shares":np.nan,"human_fill_fees":0.0});continue
        first=terminals.iloc[0];later=terminals.iloc[1:];conflict=bool((later["human_decision"]!=first["human_decision"]).any()) if not later.empty else False
        out.append({"recommendation_id":rid,"canonical_human_decision":first["human_decision"],"canonical_decision_at":first["recorded_at"],"decision_conflict":conflict,"human_fill_price":first.get("human_fill_price",np.nan),"human_fill_shares":first.get("human_fill_shares",np.nan),"human_fill_fees":float(first.get("human_fill_fees",0.0) or 0.0)})
    return pd.DataFrame(out)

def build_account_entries(gates,decisions):
    d=first_terminal_decisions(decisions);x=gates.merge(d,on="recommendation_id",how="left");x["canonical_human_decision"]=x["canonical_human_decision"].fillna("NO_DECISION")
    system=x[x["fire_state"].eq("FIRE_MANUAL")].copy();system["account"]="SYSTEM_ALL_FIRE"
    selection=system[system["canonical_human_decision"].eq("APPROVE")].copy();selection["account"]="HUMAN_SELECTION_SHADOW"
    out=pd.concat([system,selection],ignore_index=True) if not system.empty else system
    return out,x

def cohort_metrics(df,ret_col="gross_return"):
    y=pd.to_numeric(df[ret_col],errors="coerce").dropna()
    if y.empty:return {"n":0,"avg_return":None,"median_return":None,"win_rate":None,"p10_return":None,"worst_return":None,"best_return":None}
    return {"n":int(len(y)),"avg_return":float(y.mean()),"median_return":float(y.median()),"win_rate":float((y>0).mean()),"p10_return":float(y.quantile(.10)),"worst_return":float(y.min()),"best_return":float(y.max())}

@dataclass
class LiveState:
    shares:int=0;avg_cost:float=0.0;realized_pnl:float=0.0;fees:float=0.0

def apply_long_only_event(state,action,price,shares,fees=0.0):
    if not (_finite(price) and float(price)>0): raise RuntimeError("invalid trade price")
    if int(shares)<=0: raise RuntimeError("shares must be positive")
    action=str(action).upper();p=float(price);q=int(shares);fee=float(fees or 0.0)
    if action in ("ENTRY","ADD"):
        new_sh=state.shares+q;state.avg_cost=((state.avg_cost*state.shares)+(p*q))/new_sh;state.shares=new_sh;state.realized_pnl-=fee;state.fees+=fee;return state
    if action in ("REDUCE","EXIT"):
        if q>state.shares: raise RuntimeError("sell shares exceed current long position")
        state.realized_pnl+=(p-state.avg_cost)*q-fee;state.fees+=fee;state.shares-=q
        if state.shares==0: state.avg_cost=0.0
        if action=="EXIT" and state.shares!=0: raise RuntimeError("EXIT must close the full remaining position")
        return state
    raise RuntimeError(f"unsupported action {action}")

def human_live_from_ledgers(gates,decisions,trade_events,last_prices):
    d=first_terminal_decisions(decisions);approved=d[d["canonical_human_decision"].eq("APPROVE")].copy();gate_map=gates.set_index("recommendation_id").to_dict(orient="index") if not gates.empty else {};events=[]
    for _,r in approved.iterrows():
        rid=r["recommendation_id"]
        if rid not in gate_map: continue
        fp=r.get("human_fill_price",np.nan);fs=r.get("human_fill_shares",np.nan);ff=float(r.get("human_fill_fees",0.0) or 0.0)
        if _finite(fp) and _finite(fs) and int(float(fs))>0:
            meta=gate_map.get(rid,{})
            events.append({"recommendation_id":rid,"event_date":str(meta.get("fire_date",""))[:10],"recorded_at":str(r["canonical_decision_at"]),"action":"ENTRY","price":float(fp),"shares":int(float(fs)),"fees":ff,"source":"HUMAN_DECISION_LEDGER"})
    if trade_events is not None and not trade_events.empty:
        for _,r in trade_events.iterrows():events.append({"recommendation_id":str(r["recommendation_id"]),"event_date":str(r.get("trade_date",""))[:10],"recorded_at":str(r["recorded_at"]),"action":str(r["action"]).upper(),"price":float(r["fill_price"]),"shares":int(r["fill_shares"]),"fees":float(r.get("fees",0.0) or 0.0),"source":"HUMAN_TRADE_LEDGER"})
    ev=pd.DataFrame(events)
    if ev.empty:return pd.DataFrame(),pd.DataFrame(),pd.DataFrame()
    ev=ev.sort_values(["recommendation_id","event_date","recorded_at","action"]).reset_index(drop=True);positions=[];realized=[];event_rows=[]
    for rid,g in ev.groupby("recommendation_id",sort=False):
        st=LiveState()
        for _,r in g.iterrows():
            before=LiveState(st.shares,st.avg_cost,st.realized_pnl,st.fees);st=apply_long_only_event(st,r["action"],r["price"],r["shares"],r["fees"])
            event_rows.append({**r.to_dict(),"shares_before":before.shares,"shares_after":st.shares,"avg_cost_after":st.avg_cost,"realized_pnl_cum":st.realized_pnl,"fees_cum":st.fees})
        meta=gate_map.get(rid,{});px=float(last_prices.get(meta.get("symbol"),np.nan)) if last_prices else np.nan;unreal=(px-st.avg_cost)*st.shares if st.shares>0 and _finite(px) else np.nan
        positions.append({"recommendation_id":rid,"symbol":meta.get("symbol",""),"lane":meta.get("lane",""),"shares":st.shares,"avg_cost":st.avg_cost,"last_price":px,"market_value":px*st.shares if st.shares>0 and _finite(px) else np.nan,"realized_pnl":st.realized_pnl,"unrealized_pnl":unreal,"total_pnl":st.realized_pnl+(unreal if _finite(unreal) else 0.0),"fees":st.fees})
        realized.append({"recommendation_id":rid,"symbol":meta.get("symbol",""),"lane":meta.get("lane",""),"realized_pnl":st.realized_pnl,"fees":st.fees,"is_closed":st.shares==0})
    return pd.DataFrame(event_rows),pd.DataFrame(positions),pd.DataFrame(realized)
