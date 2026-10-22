from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from research.research_store import ResearchStore
from research.strategy_portfolio import (
    DEEP_ID, RET120_ID, PORTFOLIO_ID,
    StrategyPortfolioPolicy, PilotReadinessPolicy,
    combine_initial_capital_sleeves, initial_portfolio_state, max_drawdown, overlap_stats,
)

DB_PATH = PROJECT_ROOT / "data/research/positionx_research.duckdb"
OUT_DIR = PROJECT_ROOT / "data/research/strategy_portfolio_v0_1"
POLICY = StrategyPortfolioPolicy()
READINESS = PilotReadinessPolicy()


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def ensure_tables(store: ResearchStore) -> None:
    store.con.execute("""
    CREATE TABLE IF NOT EXISTS strategy_portfolio_state(
      portfolio_id VARCHAR PRIMARY KEY,
      policy_hash VARCHAR NOT NULL,
      readiness_policy_hash VARCHAR NOT NULL,
      state_json VARCHAR NOT NULL,
      updated_at TIMESTAMP DEFAULT current_timestamp
    )
    """)
    store.con.execute("""
    CREATE TABLE IF NOT EXISTS strategy_portfolio_equity(
      portfolio_id VARCHAR NOT NULL,
      trade_date VARCHAR NOT NULL,
      portfolio_equity DOUBLE NOT NULL,
      portfolio_cash DOUBLE NOT NULL,
      portfolio_gross_market_value DOUBLE NOT NULL,
      portfolio_gross_weight DOUBLE NOT NULL,
      deep_sleeve_equity DOUBLE NOT NULL,
      ret120_sleeve_equity DOUBLE NOT NULL,
      deep_contribution DOUBLE NOT NULL,
      ret120_contribution DOUBLE NOT NULL,
      PRIMARY KEY(portfolio_id, trade_date)
    )
    """)


def load_strategy_state(store: ResearchStore, strategy_id: str) -> dict[str, Any] | None:
    return store.load_state(strategy_id)


def load_curve(store: ResearchStore, strategy_id: str) -> pd.DataFrame:
    return store.con.execute("""
      SELECT trade_date,equity,cash,gross_market_value,gross_weight,active_positions
      FROM forward_equity
      WHERE strategy_id=? AND ledger='BASE20'
      ORDER BY trade_date
    """, [strategy_id]).fetchdf()


def current_positions(store: ResearchStore, strategy_id: str) -> set[str]:
    return {r[0] for r in store.con.execute(
        "SELECT symbol FROM forward_positions WHERE strategy_id=? AND ledger='BASE20'",
        [strategy_id],
    ).fetchall()}


def existing_portfolio_state(store: ResearchStore) -> dict[str, Any] | None:
    row = store.con.execute(
        "SELECT policy_hash,readiness_policy_hash,state_json FROM strategy_portfolio_state WHERE portfolio_id=?",
        [PORTFOLIO_ID],
    ).fetchone()
    if not row:
        return None
    if row[0] != POLICY.fingerprint() or row[1] != READINESS.fingerprint():
        raise RuntimeError("FAIL_CLOSED portfolio/readiness policy fingerprint drift")
    return json.loads(row[2])


def save_portfolio_state(store: ResearchStore, state: dict[str, Any]) -> None:
    store.con.execute("""
      INSERT INTO strategy_portfolio_state(portfolio_id,policy_hash,readiness_policy_hash,state_json)
      VALUES (?,?,?,?)
      ON CONFLICT(portfolio_id) DO UPDATE SET
        policy_hash=excluded.policy_hash,
        readiness_policy_hash=excluded.readiness_policy_hash,
        state_json=excluded.state_json,
        updated_at=now()
    """, [PORTFOLIO_ID, POLICY.fingerprint(), READINESS.fingerprint(), json.dumps(state, ensure_ascii=False)])


def check(db_path: Path) -> int:
    with ResearchStore(db_path) as store:
        ensure_tables(store)
        ds = load_strategy_state(store, DEEP_ID)
        rs = load_strategy_state(store, RET120_ID)
        result = {
            "status": "PASS_STRATEGY_PORTFOLIO_V0_1_CHECK",
            "portfolio_id": PORTFOLIO_ID,
            "policy": asdict(POLICY),
            "readiness_policy": asdict(READINESS),
            "deep_state_present": ds is not None,
            "ret120_state_present": rs is not None,
            "deep_status": None if ds is None else ds.get("status"),
            "ret120_status": None if rs is None else rs.get("status"),
            "research_store": str(db_path),
            "no_qmt": True,
            "no_broker": True,
            "production_db_opened": False,
        }
        if ds is None:
            result["status"] = "BLOCKED_DEEP_FORWARD_STATE_MISSING"
        if rs is None:
            result["status"] = "BLOCKED_RET120_FORWARD_STATE_MISSING"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"].startswith("PASS_") else 3


def bootstrap(db_path: Path, out_dir: Path) -> int:
    with ResearchStore(db_path) as store:
        ensure_tables(store)
        if load_strategy_state(store, DEEP_ID) is None:
            raise RuntimeError("FAIL_CLOSED Deep Forward state missing")
        if load_strategy_state(store, RET120_ID) is None:
            raise RuntimeError("FAIL_CLOSED RET120 Forward state missing; run RET120 bootstrap first")
        state = existing_portfolio_state(store) or initial_portfolio_state(POLICY, READINESS)
        save_portfolio_state(store, state)
    write_json_atomic(out_dir / "policy.json", {"portfolio": asdict(POLICY), "pilot_readiness": asdict(READINESS)})
    write_json_atomic(out_dir / "state.json", state)
    print(json.dumps({
        "status": "PASS_STRATEGY_PORTFOLIO_V0_1_BOOTSTRAP",
        "portfolio_state": state,
        "next": "RUN_SNAPSHOT_AFTER_BOTH_STRATEGIES_HAVE_COMMON_FORWARD_SESSION",
        "no_qmt": True,
        "no_broker": True,
    }, ensure_ascii=False, indent=2))
    return 0


def snapshot(db_path: Path, out_dir: Path) -> int:
    with ResearchStore(db_path) as store:
        ensure_tables(store)
        state = existing_portfolio_state(store)
        if state is None:
            raise RuntimeError("FAIL_CLOSED portfolio not bootstrapped")
        deep = load_curve(store, DEEP_ID)
        ret = load_curve(store, RET120_ID)
        combined = combine_initial_capital_sleeves(
            deep, ret,
            POLICY.deep_initial_weight, POLICY.ret120_initial_weight,
        )
        overlap = overlap_stats(current_positions(store, DEEP_ID), current_positions(store, RET120_ID))
        if combined.empty:
            state.update({
                "status": "WAITING_FOR_COMMON_FORWARD",
                "common_sessions": 0,
                "same_symbol_count": overlap["same_symbol_count"],
            })
            save_portfolio_state(store, state)
            out = {"status": "WAITING_FOR_COMMON_FORWARD", "portfolio_state": state, "overlap": overlap}
        else:
            inception = str(combined.iloc[0]["trade_date"])
            # Replace the derived table atomically inside DuckDB transaction.
            store.con.execute("BEGIN TRANSACTION")
            try:
                store.con.execute("DELETE FROM strategy_portfolio_equity WHERE portfolio_id=?", [PORTFOLIO_ID])
                rows = []
                for r in combined.to_dict("records"):
                    rows.append([
                        PORTFOLIO_ID, r["trade_date"], r["portfolio_equity"], r["portfolio_cash"],
                        r["portfolio_gross_market_value"], r["portfolio_gross_weight"],
                        r["deep_sleeve_equity"], r["ret120_sleeve_equity"],
                        r["deep_contribution"], r["ret120_contribution"],
                    ])
                store.con.executemany("""
                  INSERT INTO strategy_portfolio_equity VALUES (?,?,?,?,?,?,?,?,?,?)
                """, rows)
                state.update({
                    "status": "COLLECTING_SHADOW",
                    "started_at": state.get("started_at") or inception,
                    "last_aggregated_date": str(combined.iloc[-1]["trade_date"]),
                    "common_sessions": int(len(combined)),
                    "max_drawdown": max_drawdown(combined["portfolio_equity"]),
                    "latest_equity": float(combined.iloc[-1]["portfolio_equity"]),
                    "latest_gross_weight": float(combined.iloc[-1]["portfolio_gross_weight"]),
                    "same_symbol_count": overlap["same_symbol_count"],
                })
                save_portfolio_state(store, state)
                store.con.execute("COMMIT")
            except Exception:
                store.con.execute("ROLLBACK")
                raise
            out = {
                "status": "PASS_STRATEGY_PORTFOLIO_V0_1_SNAPSHOT",
                "portfolio_state": state,
                "latest": combined.iloc[-1].to_dict(),
                "overlap": overlap,
                "methodology": "50/50 initial-capital sleeves; no rebalance; portfolio inception is first common forward session",
            }
    write_json_atomic(out_dir / "state.json", state)
    write_json_atomic(out_dir / "latest.json", out)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def status(db_path: Path) -> int:
    with ResearchStore(db_path) as store:
        ensure_tables(store)
        state = existing_portfolio_state(store)
        if state is None:
            print(json.dumps({"status": "NOT_BOOTSTRAPPED", "portfolio_id": PORTFOLIO_ID}, ensure_ascii=False, indent=2))
            return 0
        print(json.dumps({"status": "PASS_STRATEGY_PORTFOLIO_V0_1_STATUS", "state": state}, ensure_ascii=False, indent=2))
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description="PositionX Strategy Portfolio V0.1 shadow aggregator")
    p.add_argument("command", choices=["check", "bootstrap", "snapshot", "status"])
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--output-dir", default=str(OUT_DIR))
    p.add_argument("--recovery-profile")
    p.add_argument("--expected-trade-date")
    a = p.parse_args()
    try:
        if a.recovery_profile:
            from scripts.p0_core_recovery_v01 import load_profile, check_portfolio, portfolio_snapshot, portfolio_status
            profile = load_profile(a.recovery_profile)
            operations = {"check": check_portfolio, "snapshot": portfolio_snapshot, "status": portfolio_status}
            if a.command not in operations or not a.expected_trade_date:
                raise RuntimeError("FAIL_CLOSED RECOVERY_DATE_AND_OPERATION_REQUIRED")
            print(json.dumps(operations[a.command](profile, a.expected_trade_date), allow_nan=False))
            return 0
        if a.command == "check": return check(Path(a.db))
        if a.command == "bootstrap": return bootstrap(Path(a.db), Path(a.output_dir))
        if a.command == "snapshot": return snapshot(Path(a.db), Path(a.output_dir))
        return status(Path(a.db))
    except Exception as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
