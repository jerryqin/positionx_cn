from __future__ import annotations

import argparse, json, os, sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import duckdb

from research.pilot_risk_recovery import (
    PilotRiskRecoveryPolicy, RiskInputs, evaluate_new_entry,
    sha256_file, copy_verify,
)

RESEARCH_DB = PROJECT_ROOT / "data/research/positionx_research.duckdb"
PRODUCTION_DB = PROJECT_ROOT / "data/positionx.db"
OUT_DIR = Path(r"D:\positionx_data\research\pilot_live_readiness\risk_recovery_v0_1")
RESTORE_ROOT = Path(r"D:\positionx_data\restore_test\pilot_live_readiness\risk_recovery_v0_1")
EXECUTION_SOURCE = PROJECT_ROOT / "core/execution.py"
DB_SOURCE = PROJECT_ROOT / "core/db.py"

POLICY = PilotRiskRecoveryPolicy()

def source_guard_checks() -> dict:
    ex = EXECUTION_SOURCE.read_text(encoding="utf-8", errors="ignore") if EXECUTION_SOURCE.exists() else ""
    db = DB_SOURCE.read_text(encoding="utf-8", errors="ignore") if DB_SOURCE.exists() else ""
    return {
        "execution_source_exists": EXECUTION_SOURCE.exists(),
        "db_source_exists": DB_SOURCE.exists(),
        "pilot_limit_present": "_pilot_limit" in ex and "pilot_max_order_value" in ex,
        "duplicate_daily_intent_counter_present": "count_order_intents_for_date" in db,
        "intent_key_guard_present": "intent_key" in db,
    }

def failure_injection_checks() -> dict:
    cases = {
        "healthy": RiskInputs(proposed_target_gross=0.30, manual_approval=True),
        "stale_data": RiskInputs(fresh_market_data=False),
        "missing_pit": RiskInputs(pit_snapshot_complete=False),
        "missed_session": RiskInputs(missed_forward_session=True),
        "policy_mismatch": RiskInputs(policy_hash_match=False),
        "reconcile_mismatch": RiskInputs(reconcile_clean=False),
        "duplicate_intent": RiskInputs(duplicate_intent=True),
        "gross_breach": RiskInputs(proposed_target_gross=0.300001),
        "missing_manual_approval": RiskInputs(manual_approval=False),
    }
    out = {k: evaluate_new_entry(v, POLICY) for k, v in cases.items()}
    checks = {
        "healthy_allows": out["healthy"]["allow_new_entry"] is True,
        "every_injected_failure_blocks": all(
            out[k]["allow_new_entry"] is False for k in out if k != "healthy"
        ),
        "no_failure_forces_liquidation": all(x["force_liquidation"] is False for x in out.values()),
    }
    return {"cases": out, "checks": checks}

def checkpoint_and_recovery_drill() -> dict:
    if not RESEARCH_DB.exists():
        raise RuntimeError(f"missing research db: {RESEARCH_DB}")
    RESTORE_ROOT.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(RESEARCH_DB))
    try:
        con.execute("CHECKPOINT")
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name"
        ).fetchall()]
    finally:
        con.close()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    restore_dir = RESTORE_ROOT / ts
    restore_dir.mkdir(parents=True, exist_ok=True)
    db_copy = restore_dir / RESEARCH_DB.name
    copy_result = copy_verify(RESEARCH_DB, db_copy)

    rcon = duckdb.connect(str(db_copy), read_only=True)
    try:
        restored_tables = [r[0] for r in rcon.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name"
        ).fetchall()]
    finally:
        rcon.close()

    state_files = []
    for rel in [
        "data/research/forward_deep_oversold_csi500_v1_1/state.json",
        "data/research/forward_ret120_csi500_v1_0/state.json",
    ]:
        p = PROJECT_ROOT / rel
        if p.exists():
            tag = p.parent.name + "_state.json"
            state_files.append(copy_verify(p, restore_dir / tag))

    return {
        "restore_dir": str(restore_dir),
        "research_db": copy_result,
        "tables_before": tables,
        "tables_restored": restored_tables,
        "table_set_match": tables == restored_tables,
        "state_file_copies": state_files,
        "all_state_hashes_match": all(x["match"] for x in state_files),
    }

def write_json_atomic(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)

def check() -> int:
    src = source_guard_checks()
    passed = RESEARCH_DB.exists() and all(src.values())
    result = {
        "status": "PASS_PILOT_RISK_RECOVERY_V0_1_CHECK" if passed else "BLOCKED_PILOT_RISK_RECOVERY_V0_1_CHECK",
        "policy": POLICY.__dict__,
        "policy_hash": POLICY.fingerprint(),
        "research_db_exists": RESEARCH_DB.exists(),
        "production_db_exists": PRODUCTION_DB.exists(),
        "source_guards": src,
        "no_broker": True,
        "no_qmt": True,
        "auto_execute": False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if passed else 3

def run() -> int:
    prod_before = sha256_file(PRODUCTION_DB) if PRODUCTION_DB.exists() else None
    src = source_guard_checks()
    inj = failure_injection_checks()
    recovery = checkpoint_and_recovery_drill()
    prod_after = sha256_file(PRODUCTION_DB) if PRODUCTION_DB.exists() else None

    checks = {
        "source_guards_present": all(src.values()),
        "healthy_case_allows_new_entry": inj["checks"]["healthy_allows"],
        "all_failure_injections_stop_new_entries": inj["checks"]["every_injected_failure_blocks"],
        "integrity_failure_never_forces_liquidation": inj["checks"]["no_failure_forces_liquidation"],
        "research_db_copy_hash_match": recovery["research_db"]["match"],
        "research_db_table_set_match": recovery["table_set_match"],
        "forward_state_copy_hashes_match": recovery["all_state_hashes_match"],
        "restored_research_db_readable": len(recovery["tables_restored"]) > 0,
        "production_sqlite_unchanged": prod_before == prod_after,
    }
    passed = all(checks.values())
    result = {
        "status": "PASS_PILOT_RISK_RECOVERY_AND_KILL_SWITCH_V0_1" if passed else "BLOCKED_PILOT_RISK_RECOVERY_AND_KILL_SWITCH_V0_1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "policy": POLICY.__dict__,
        "policy_hash": POLICY.fingerprint(),
        "kill_switch_semantics": {
            "integrity_or_reconcile_failure": "STOP_NEW_ENTRIES",
            "existing_positions": "DO_NOT_FORCE_LIQUIDATE; continue only frozen exit rules with fresh valid data, otherwise human review",
            "portfolio_target_gross": "<=30% at new-entry decision time",
            "duplicate_order_intent": "BLOCK",
            "manual_approval": "REQUIRED",
            "auto_execute": False,
            "max_rmb_loss": "NOT_SET_HERE; G7 user-owned decision",
        },
        "source_guards": src,
        "failure_injection": inj,
        "recovery_drill": recovery,
        "production_db_sha256_before": prod_before,
        "production_db_sha256_after": prod_after,
        "checks": checks,
        "no_broker": True,
        "no_qmt": True,
        "production_db_mutation": False,
    }
    write_json_atomic(OUT_DIR / "risk_recovery_summary_v01.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"OUTPUT: {OUT_DIR}")
    return 0 if passed else 3

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["check", "run"], nargs="?", default="check")
    a = p.parse_args()
    try:
        return check() if a.command == "check" else run()
    except Exception as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
        return 3

if __name__ == "__main__":
    raise SystemExit(main())
