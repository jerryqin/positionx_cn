from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Iterable
import hashlib, json

POLICY_ID = "G4_SHADOW_SAMPLING_CONTRACT_V0_1"
SALT = "POSITIONX|G4|SHADOW_TO_EXECUTION_PILOT|V0.1"
CANARY_DAILY_SAMPLE = 1
EXPANDED_DAILY_SAMPLE = 2
MAX_PILOT_ORDER_VALUE_RMB = 50_000.0
SHADOW_ECONOMIC_BOOK_RMB = 12_000_000.0

@dataclass(frozen=True)
class ShadowOrderDecision:
    shadow_decision_id: str
    trade_date: str
    strategy_id: str
    symbol: str
    side: str
    shadow_target_notional_rmb: float
    shadow_target_weight: float | None = None
    source_key: str | None = None

    def normalized(self) -> "ShadowOrderDecision":
        side = self.side.upper().strip()
        if side not in ("BUY","SELL"):
            raise ValueError(f"invalid side={self.side}")
        if float(self.shadow_target_notional_rmb) <= 0:
            raise ValueError("shadow_target_notional_rmb must be >0")
        return ShadowOrderDecision(
            shadow_decision_id=str(self.shadow_decision_id).strip(),
            trade_date=str(self.trade_date).replace("-","").strip(),
            strategy_id=str(self.strategy_id).strip(),
            symbol=str(self.symbol).upper().strip(),
            side=side,
            shadow_target_notional_rmb=float(self.shadow_target_notional_rmb),
            shadow_target_weight=None if self.shadow_target_weight is None else float(self.shadow_target_weight),
            source_key=None if self.source_key is None else str(self.source_key),
        )

def sampling_material(d: ShadowOrderDecision) -> str:
    x=d.normalized()
    # IMPORTANT: no price, return, ADV, volatility, rank, PnL, or future data.
    return "|".join([
        SALT, x.trade_date, x.strategy_id, x.symbol, x.side,
        x.shadow_decision_id, x.source_key or "",
    ])

def sample_hash(d: ShadowOrderDecision) -> str:
    return hashlib.sha256(sampling_material(d).encode("utf-8")).hexdigest()

def pilot_target_notional(d: ShadowOrderDecision) -> float:
    # Never enlarge a shadow economic order. 50k is representative-ticket ceiling.
    x=d.normalized()
    return min(MAX_PILOT_ORDER_VALUE_RMB, x.shadow_target_notional_rmb)

def deterministic_sample(decisions: Iterable[ShadowOrderDecision], stage: str) -> list[dict]:
    stage=stage.upper().strip()
    if stage not in ("CANARY","EXPANDED"):
        raise ValueError(f"unknown stage={stage}")
    limit=CANARY_DAILY_SAMPLE if stage=="CANARY" else EXPANDED_DAILY_SAMPLE

    xs=[d.normalized() for d in decisions]
    if not xs:
        return []
    dates={d.trade_date for d in xs}
    if len(dates)!=1:
        raise ValueError("deterministic_sample requires exactly one trade_date")

    # Deduplicate exact decision IDs fail-closed.
    ids=[d.shadow_decision_id for d in xs]
    if len(ids)!=len(set(ids)):
        raise ValueError("duplicate shadow_decision_id")

    ranked=sorted(xs, key=lambda d:(sample_hash(d), d.strategy_id, d.symbol, d.side, d.shadow_decision_id))

    selected=[]
    used_symbols=set()
    for d in ranked:
        # Avoid spending two Pilot slots on the same underlying in one session.
        # This uses symbol only, not alpha/price/outcome data.
        if d.symbol in used_symbols:
            continue
        selected.append({
            **asdict(d),
            "policy_id":POLICY_ID,
            "stage":stage,
            "sample_hash":sample_hash(d),
            "pilot_target_notional_rmb":pilot_target_notional(d),
            "sample_rank":len(selected)+1,
            "selected":True,
        })
        used_symbols.add(d.symbol)
        if len(selected)>=limit:
            break
    return selected

def stable_decision_id(trade_date:str, strategy_id:str, symbol:str, side:str, source_key:str) -> str:
    material="|".join([POLICY_ID,str(trade_date).replace("-",""),strategy_id,symbol.upper(),side.upper(),source_key])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]

def candidate_fingerprint(rows:list[dict]) -> str:
    canonical=json.dumps(rows,sort_keys=True,ensure_ascii=False,separators=(",",":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
