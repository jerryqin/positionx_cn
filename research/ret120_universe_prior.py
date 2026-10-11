from __future__ import annotations
from typing import Any
TIERS=("CSI300","CSI500","CSI1000","EX_CSI1800")
BUCKETS=("Q1","Q2","Q3","Q4","Q5")

def _base():
    return {
        "momentum_candidate":False,
        "momentum_confidence":None,
        "momentum_penalty":False,
        "mean_reversion_candidate":False,
        "preferred_horizon":None,
        "action_semantics":"CANDIDATE_ONLY_NOT_ACTION",
    }

def classify_ret120_prior(universe_tier:str,bucket:str)->dict[str,Any]:
    if universe_tier not in TIERS: raise ValueError(f"unsupported universe_tier={universe_tier}")
    if bucket not in BUCKETS: raise ValueError(f"unsupported bucket={bucket}")
    p=_base()
    if universe_tier in ("CSI300","CSI500") and bucket=="Q5":
        p.update(momentum_candidate=True,momentum_confidence="STRONG",preferred_horizon="60_120D")
    elif universe_tier=="CSI1000" and bucket=="Q5":
        p.update(momentum_candidate=True,momentum_confidence="MEDIUM",preferred_horizon="120D")
    elif universe_tier=="EX_CSI1800" and bucket=="Q5":
        p.update(momentum_candidate=False,momentum_penalty=True,preferred_horizon="20_120D")
    elif universe_tier=="EX_CSI1800" and bucket=="Q1":
        p.update(mean_reversion_candidate=True,action_semantics="CANDIDATE_ONLY_NOT_BUY_SIGNAL")
    return p
