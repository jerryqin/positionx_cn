from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
import hashlib, json, shutil, sqlite3

PORTFOLIO_MAX_TARGET_GROSS = 0.30

@dataclass(frozen=True)
class PilotRiskRecoveryPolicy:
    policy_id: str = "PILOT_RISK_RECOVERY_V0_1"
    portfolio_max_target_gross: float = PORTFOLIO_MAX_TARGET_GROSS
    require_manual_approval: bool = True
    auto_execute: bool = False
    block_on_stale_or_missing_data: bool = True
    block_on_missed_forward_session: bool = True
    block_on_policy_hash_mismatch: bool = True
    block_on_unexplained_reconcile_mismatch: bool = True
    block_on_duplicate_order_intent: bool = True
    block_on_target_gross_breach: bool = True
    force_liquidation_on_integrity_failure: bool = False

    def fingerprint(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

@dataclass
class RiskInputs:
    fresh_market_data: bool = True
    pit_snapshot_complete: bool = True
    missed_forward_session: bool = False
    policy_hash_match: bool = True
    reconcile_clean: bool = True
    duplicate_intent: bool = False
    proposed_target_gross: float = 0.0
    manual_approval: bool = True

def evaluate_new_entry(inp: RiskInputs, policy: PilotRiskRecoveryPolicy | None = None) -> dict[str, Any]:
    p = policy or PilotRiskRecoveryPolicy()
    reasons: list[str] = []
    if p.block_on_stale_or_missing_data and (not inp.fresh_market_data or not inp.pit_snapshot_complete):
        reasons.append("STALE_OR_INCOMPLETE_DATA")
    if p.block_on_missed_forward_session and inp.missed_forward_session:
        reasons.append("MISSED_FORWARD_SESSION")
    if p.block_on_policy_hash_mismatch and not inp.policy_hash_match:
        reasons.append("POLICY_HASH_MISMATCH")
    if p.block_on_unexplained_reconcile_mismatch and not inp.reconcile_clean:
        reasons.append("UNEXPLAINED_RECONCILE_MISMATCH")
    if p.block_on_duplicate_order_intent and inp.duplicate_intent:
        reasons.append("DUPLICATE_ORDER_INTENT")
    if p.block_on_target_gross_breach and inp.proposed_target_gross > p.portfolio_max_target_gross + 1e-12:
        reasons.append("PORTFOLIO_TARGET_GROSS_BREACH")
    if p.require_manual_approval and not inp.manual_approval:
        reasons.append("MANUAL_APPROVAL_MISSING")
    return {
        "allow_new_entry": len(reasons) == 0,
        "action": "ALLOW_NEW_ENTRY" if not reasons else "STOP_NEW_ENTRIES",
        "reasons": reasons,
        "force_liquidation": False,
        "policy_id": p.policy_id,
        "policy_hash": p.fingerprint(),
    }

def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def copy_verify(src: Path, dst: Path) -> dict[str, Any]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    a, b = sha256_file(src), sha256_file(dst)
    return {"source": str(src), "copy": str(dst), "source_sha256": a, "copy_sha256": b, "match": a == b}

def sqlite_readable(path: Path) -> bool:
    if not path.exists():
        return False
    uri = f"file:{path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        con.execute("PRAGMA schema_version").fetchone()
        return True
    finally:
        con.close()
