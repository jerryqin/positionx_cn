from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.research_store import ResearchStore
from research.strategy_portfolio import (
    DEEP_ID, RET120_ID, PORTFOLIO_ID,
    PilotReadinessPolicy, readiness_summary,
)

DB_PATH = PROJECT_ROOT / "data/research/positionx_research.duckdb"
POLICY = PilotReadinessPolicy()
QUAL = Path(r"D:\positionx_data\research\strategy2_ret120_qualification_v0_1\strategy2_qualification_v01.json")
SEM = Path(r"D:\positionx_data\research\strategy2_ret120_forward_semantics_v0_1\ret120_forward_execution_semantics_v01.json")
G6 = Path(r"D:\positionx_data\research\pilot_live_readiness\risk_recovery_v0_1\risk_recovery_summary_v01.json")


def _read(path: Path) -> dict:
    if not path.exists(): return {}
    try: return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception: return {}


def _table_exists(con, name: str) -> bool:
    return bool(con.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='main' AND table_name=?", [name]).fetchone())


def audit(db_path: Path) -> int:
    qual, sem, g6 = _read(QUAL), _read(SEM), _read(G6)
    from core.qmt_runtime import is_full,segment_bindings
    if is_full():
        import duckdb
        paths=segment_bindings();states=[]
        for sid,key in ((DEEP_ID,'deep_db'),(RET120_ID,'ret120_db')):
            con=duckdb.connect(paths[key],read_only=True)
            try:
                row=con.execute('SELECT state_json FROM forward_state WHERE strategy_id=?',[sid]).fetchone()
                states.append(json.loads(row[0]) if row else {})
            finally:con.close()
        ds,rs=states
        con=duckdb.connect(paths['portfolio_db'],read_only=True)
        try:
            row=con.execute('SELECT count(*),max(trade_date) FROM p0_portfolio_equity').fetchone() if _table_exists(con,'p0_portfolio_equity') else (0,None)
            portfolio_sessions=int(row[0]);common_sessions=portfolio_sessions
            portfolio_state={'last_processed_date':row[1],'common_sessions':common_sessions,'segment_start':paths['start_date'],'legacy_returns_spliced':False}
        finally:con.close()
    else:
        with ResearchStore(db_path) as store:
            ds = store.load_state(DEEP_ID) or {}
            rs = store.load_state(RET120_ID) or {}
            portfolio_state = {}
            if _table_exists(store.con, "strategy_portfolio_state"):
                row = store.con.execute("SELECT state_json FROM strategy_portfolio_state WHERE portfolio_id=?", [PORTFOLIO_ID]).fetchone()
                portfolio_state = json.loads(row[0]) if row else {}
            common_sessions = int(portfolio_state.get("common_sessions", 0) or 0)
            portfolio_sessions = int(store.con.execute(
                "SELECT count(*) FROM strategy_portfolio_equity WHERE portfolio_id=?", [PORTFOLIO_ID]
            ).fetchone()[0]) if _table_exists(store.con, "strategy_portfolio_equity") else 0

    evidence_ok = (
        qual.get("status") == "QUALIFIED_STRATEGY_2_HISTORICAL_EVIDENCE_V0_1"
        and sem.get("status") == "PASS_RET120_FORWARD_EXECUTION_SEMANTICS_AUDIT_V0_1"
    )
    risk_ok = g6.get("status") == "PASS_PILOT_RISK_RECOVERY_AND_KILL_SWITCH_V0_1"
    r = readiness_summary(
        common_sessions=common_sessions,
        portfolio_sessions=portfolio_sessions,
        shadow_order_decisions=0,
        board_lot_fidelity=None,
        execution_equivalence_pass=False,
        risk_recovery_pass=risk_ok,
        pilot_policy_frozen=False,
        policy=POLICY,
    )
    if not evidence_ok:
        r["gates"]["G1_STRATEGY_EVIDENCE"] = {"status": "BLOCKED"}
        r["ready"] = False
        r["status"] = "PILOT_LIVE_READINESS_BLOCKED"
    r["forward_states"] = {
        "deep": {k: ds.get(k) for k in ("status","started_at","last_processed_date","completed_trades","active_positions")},
        "ret120": {k: rs.get(k) for k in ("status","started_at","last_processed_date","completed_trades","active_positions")},
        "portfolio": portfolio_state,
    }
    r["g6_evidence"] = {"path": str(G6), "status": g6.get("status")}
    r["next_work_packages"] = [
        "RET120_TRUE_FORWARD_START",
        "STRATEGY_PORTFOLIO_SHADOW_20_COMMON_SESSIONS",
        "PILOT_CAPITAL_FLOOR_POLICY_FREEZE",
        "SHADOW_ORDER_EXECUTION_EQUIVALENCE",
        "PILOT_POLICY_FREEZE_WITH_USER_LOSS_BUDGET",
    ]
    r["policy"] = asdict(POLICY)
    r["no_qmt"] = True
    r["no_broker"] = True
    r["auto_execute"] = False
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument("command", choices=["audit"], nargs="?", default="audit")
    p.add_argument("--db", default=str(DB_PATH))
    a=p.parse_args()
    try: return audit(Path(a.db))
    except Exception as exc:
        print(json.dumps({"status":"FAIL_CLOSED","error":f"{type(exc).__name__}: {exc}"},ensure_ascii=False,indent=2)); return 3

if __name__ == "__main__": raise SystemExit(main())
