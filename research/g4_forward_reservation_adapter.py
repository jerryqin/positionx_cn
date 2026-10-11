from __future__ import annotations
from dataclasses import dataclass, asdict
from itertools import combinations
from typing import Iterable
import hashlib, json

POLICY_ID="G4_FORWARD_RESERVATION_ADAPTER_V0_2"
SALT="POSITIONX|G4|FORWARD_RESERVATION|V0.2"
CANARY_LIMIT=1
EXPANDED_LIMIT=2

DEEP_ID="DEEP_OVERSOLD_CSI500_FORWARD_V1_1"
RET120_ID="RET120_CSI500_FORWARD_V1_0"

@dataclass(frozen=True)
class PendingSignal:
    strategy_id:str
    symbol:str
    signal_date:str
    status:str
    reason:str|None=None
    source_table:str|None=None

    def normalized(self):
        return PendingSignal(
            strategy_id=str(self.strategy_id).strip(),
            symbol=str(self.symbol).upper().strip(),
            signal_date=str(self.signal_date).replace("-","").strip(),
            status=str(self.status).strip(),
            reason=None if self.reason is None else str(self.reason),
            source_table=None if self.source_table is None else str(self.source_table),
        )

def source_key(x:PendingSignal)->str:
    x=x.normalized()
    return "|".join([x.source_table or "",x.signal_date,x.strategy_id,x.symbol,x.status])

def decision_id(x:PendingSignal)->str:
    x=x.normalized()
    material="|".join([POLICY_ID,x.signal_date,x.strategy_id,x.symbol,"BUY",source_key(x)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]

def reservation_hash(x:PendingSignal)->str:
    x=x.normalized()
    # Deliberately excludes price, return, ADV, volatility, rank, PnL and future outcomes.
    material="|".join([SALT,x.signal_date,x.strategy_id,x.symbol,"BUY",decision_id(x),source_key(x)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()

def infer_pending_statuses(status_counts:dict[str,int], expected_pending:int)->list[str]:
    """Infer pending status labels only when count reconciliation has exactly one solution."""
    expected=int(expected_pending)
    clean={str(k):int(v) for k,v in status_counts.items() if int(v)>0}
    if expected<0:
        raise ValueError("expected_pending<0")
    if expected==0:
        return []
    labels=sorted(clean)
    sols=[]
    for n in range(1,len(labels)+1):
        for combo in combinations(labels,n):
            if sum(clean[s] for s in combo)==expected:
                sols.append(list(combo))
                if len(sols)>1:
                    raise RuntimeError(f"AMBIGUOUS_PENDING_STATUS_MAPPING expected={expected} solutions={sols[:2]}")
    if not sols:
        raise RuntimeError(f"NO_PENDING_STATUS_MAPPING expected={expected} status_counts={clean}")
    return sols[0]

def reserve(signals:Iterable[PendingSignal], stage:str)->list[dict]:
    stage=stage.upper().strip()
    if stage not in ("CANARY","EXPANDED"):
        raise ValueError(f"invalid stage={stage}")
    limit=CANARY_LIMIT if stage=="CANARY" else EXPANDED_LIMIT
    xs=[x.normalized() for x in signals]
    if not xs:return []
    dates={x.signal_date for x in xs}
    if len(dates)!=1:
        raise ValueError("reservation requires one signal_date")
    ids=[decision_id(x) for x in xs]
    if len(ids)!=len(set(ids)):
        raise ValueError("duplicate shadow_decision_id")
    ranked=sorted(xs,key=lambda x:(reservation_hash(x),x.strategy_id,x.symbol,decision_id(x)))
    out=[]; used_symbols=set()
    for x in ranked:
        if x.symbol in used_symbols:
            continue
        out.append({
            **asdict(x),
            "shadow_decision_id":decision_id(x),
            "sample_hash":reservation_hash(x),
            "sample_rank":len(out)+1,
            "stage":stage,
            "reservation_status":"RESERVED_AWAIT_T1_OPEN_CONFIRMATION",
            "confirmed_shadow_notional_rmb":None,
            "pilot_target_notional_rmb":None,
            "selected":True,
        })
        used_symbols.add(x.symbol)
        if len(out)>=limit:break
    return out

def fingerprint(rows:list[dict])->str:
    raw=json.dumps(rows,sort_keys=True,ensure_ascii=False,separators=(",",":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
