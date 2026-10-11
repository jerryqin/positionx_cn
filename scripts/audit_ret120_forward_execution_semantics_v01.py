from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(r"D:\positionx_min_live")
DATA_ROOT = Path(r"D:\positionx_data")
QUAL_DIR = DATA_ROOT / "research/strategy2_ret120_qualification_v0_1"
QUAL_JSON = QUAL_DIR / "strategy2_qualification_v01.json"
DESIGN_JSON = QUAL_DIR / "ret120_forward_protocol_design_v01.json"
P1_SCRIPT = ROOT / "scripts/audit_ret120_frozen_signal_comparable_v02.py"
P1_OUT = DATA_ROOT / "research/ret120_vs_52wh_family_v0_2_frozen_signal"
P1_SUMMARY = P1_OUT / "ret120_comparable_summary.json"
P1_Q5 = P1_OUT / "ret120_q5_candidate_events.csv"
CATALOG = DATA_ROOT / "catalog/market_data.duckdb"
STATUS_PARQUET = Path(
    r"D:\positionx_data\incoming\eva\20260925_foundation_assets\data\research\px_assets\a_share_daily_trading_status.parquet"
)
OUT_DIR = DATA_ROOT / "research/strategy2_ret120_forward_semantics_v0_1"

EXPECTED_QUAL = "QUALIFIED_STRATEGY_2_HISTORICAL_EVIDENCE_V0_1"
EXPECTED_DESIGN = "FORWARD_PROTOCOL_DESIGN_FROZEN_PENDING_EXECUTION_SEMANTICS_AUDIT_V0_1"
EXPECTED_P1 = "PASS_RET120_FROZEN_SIGNAL_COMPARABLE_PORTFOLIO_AUDIT_V0_2"
EXPECTED_STATUS_SHA = "bfe788d755fabb03986635d975cab22f1311af3a82ec533eb244ab85560c5bf2"
PASS_STATUS = "PASS_RET120_FORWARD_EXECUTION_SEMANTICS_AUDIT_V0_1"
LATEST = "20260930"
SIGNAL_START = "20240101"
SIGNAL_CUTOFF = "20260408"

# Predeclared before seeing the P4b result. These are semantic-drift guardrails,
# not strategy optimization thresholds.
MAX_DELAYED_EXECUTED_FRACTION = 0.05
MAX_ABS_CAGR_DRIFT = 0.005      # 50 bp annualized
MAX_ABS_MDD_DRIFT = 0.005       # 50 bp drawdown
MAX_ABS_SHARPE_DRIFT = 0.10

ST_RE = re.compile(r"^(?:\*?ST|S\*?ST|SST|NST)", re.I)


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise RuntimeError("FAIL_CLOSED " + msg)


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    require(path.exists(), f"missing json: {path}")
    try:
        x = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        raise RuntimeError(f"FAIL_CLOSED invalid json {path}: {e}") from e
    require(isinstance(x, dict), f"expected object json: {path}")
    return x


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_module(path: Path, name: str):
    require(path.exists(), f"missing source: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, f"cannot import: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def clean_name(v: Any) -> str:
    return str(v or "").replace(" ", "").replace("　", "").replace("＊", "*").strip()


def is_st_name(v: Any) -> bool:
    # Same observed-current-name semantics already used by PositionX full-market intake.
    return bool(ST_RE.match(clean_name(v).upper()))


def standard_open_v1(
    open_price: float | None,
    up_stop_price: float | None,
    instrument_status: int | None,
    price_tick: float | None = None,
    high_price: float | None = None,
    low_price: float | None = None,
    close_price: float | None = None,
    prev_close: float | None = None,
) -> dict[str, Any]:
    """Forward entry predicate with the same economic meaning as historical can_buy_at_open.

    This intentionally matches the already-frozen Deep Forward STANDARD_OPEN_V1:
    valid open; not suspended; not open-at-up-limit; and, when no limit value is
    available, reject a completed-bar one-price-up lock.
    """
    if open_price is None or not math.isfinite(float(open_price)) or float(open_price) <= 0:
        return {"fillable": False, "reason": "NO_VALID_OPEN"}
    if instrument_status is not None and int(instrument_status) >= 1:
        return {"fillable": False, "reason": f"SUSPENDED_STATUS_{int(instrument_status)}"}
    up_known = (
        up_stop_price is not None
        and math.isfinite(float(up_stop_price))
        and float(up_stop_price) > 0
    )
    if up_known:
        tick = float(price_tick or 0.01)
        if float(open_price) >= float(up_stop_price) - tick / 2.0:
            return {"fillable": False, "reason": "OPEN_AT_UP_LIMIT"}
        return {"fillable": True, "reason": "STANDARD_OPEN_V1_LIMITED"}
    vals = (high_price, low_price, close_price, prev_close)
    if all(v is not None and math.isfinite(float(v)) for v in vals):
        if abs(float(high_price) - float(low_price)) < 1e-12 and float(close_price) > float(prev_close):
            return {"fillable": False, "reason": "ONE_PRICE_UP_NO_LIMIT_DATA"}
    return {"fillable": True, "reason": "STANDARD_OPEN_V1_NO_LIMIT"}


def historical_can_buy_formula(row: pd.Series) -> bool:
    vals = [row.get("open"), row.get("high"), row.get("low"), row.get("close"), row.get("pre_close")]
    def finite(x):
        try: return x is not None and math.isfinite(float(x))
        except Exception: return False
    if bool(row.get("is_suspended")):
        return False
    if not finite(row.get("open")) or float(row.get("open")) <= 0:
        return False
    one_price_up = all(finite(x) for x in vals[1:]) and abs(float(row["high"])-float(row["low"])) < 1e-12 and float(row["close"]) > float(row["pre_close"])
    if one_price_up:
        return False
    up = row.get("up_limit")
    if finite(up) and float(row["open"]) >= float(up) - 1e-12:
        return False
    return True


def review_gate() -> dict[str, Any]:
    # 30 slots / 120-session holding => ~30 completed positions per turnover wave.
    # Two nominal waves = 60 completed trades and ~240 trading sessions ≈ 12 months.
    return {
        "completed_trades_target": 60,
        "calendar_months_target": 12,
        "logic": "completed_trades>=60 AND calendar_months>=12",
        "rationale": (
            "RET120 has 30 max positions and a 120-session hold. Two nominal slot-turnover "
            "waves provide ~60 completed trades; ~240 trading sessions is approximately 12 calendar months."
        ),
        "early_health_checks": "allowed, but cannot promote/retire strategy before the formal gate solely from Forward PnL",
    }


def resolved_forward_policy() -> dict[str, Any]:
    return {
        "strategy_id": "RET120_CSI500_FORWARD_V1_0",
        "entry_fillability": {
            "signal_day": [
                "exact completed T bar required",
                "observed InstrumentStatus must not indicate suspension when available",
                "known current ST name excluded; missing/unknown ST name retained but reported",
                "new-listing-special explicitly excluded; ret120 121-observation history makes this normally redundant",
            ],
            "entry_day": [
                "exact positive T+1 OPEN required",
                "known current ST name excluded; unknown retained but reported",
                "new-listing-special excluded",
                "STANDARD_OPEN_V1: not suspended, not one-price-up locked, and open below UpStopPrice when known",
            ],
            "qmt_fields": ["InstrumentName", "OpenDate", "InstrumentStatus", "UpStopPrice", "PriceTick"],
            "bar_fields": ["open", "high", "low", "close", "preClose/same-source previous close"],
            "unknown_st_policy": "RETAIN_AND_REPORT, matching historical known-vs-unknown convention",
        },
        "exit": {
            "planned": "S120 CLOSE",
            "if_s120_tradable": "exit at exact S120 CLOSE",
            "if_s120_suspended_or_no_valid_close": (
                "defer; remain an active position and continue consuming slot/capital until the first subsequent session "
                "with a real valid CLOSE; exit at that CLOSE"
            ),
            "same_day_close_cash_reusable_at_open": False,
            "synthetic_or_last_price_execution": "FORBIDDEN",
            "silent_position_drop": "FORBIDDEN",
            "terminal_delisting_or_never_resumes": "FAIL_CLOSED_EXCEPTION_REVIEW; do not invent a zero/last-price execution",
        },
        "review_gate": review_gate(),
        "research_only": True,
        "broker": False,
        "auto_execute": False,
    }


def verify_static_inputs() -> dict[str, Any]:
    for p in (QUAL_JSON, DESIGN_JSON, P1_SCRIPT, P1_SUMMARY, P1_Q5, CATALOG, STATUS_PARQUET):
        require(p.exists(), f"missing input: {p}")
    require(sha256_file(STATUS_PARQUET) == EXPECTED_STATUS_SHA, "historical status asset hash drift")
    q = read_json(QUAL_JSON)
    d = read_json(DESIGN_JSON)
    p1s = read_json(P1_SUMMARY)
    require(q.get("status") == EXPECTED_QUAL, f"qualification status={q.get('status')}")
    require(d.get("status") == EXPECTED_DESIGN, f"design status={d.get('status')}")
    require(q.get("forward_status") in (None, "NOT_STARTED_PENDING_EXECUTION_SEMANTICS_AUDIT"), "unexpected forward status")
    require(p1s.get("status") == EXPECTED_P1, f"P1 status={p1s.get('status')}")
    return {
        "qualification_sha256": sha256_file(QUAL_JSON),
        "design_sha256": sha256_file(DESIGN_JSON),
        "p1_summary_sha256": sha256_file(P1_SUMMARY),
        "p1_q5_sha256": sha256_file(P1_Q5),
        "status_sha256": sha256_file(STATUS_PARQUET),
    }


def audit_historical_can_buy_equivalence(p1) -> dict[str, Any]:
    # Use actual P1 Q5 entry keys and the exact frozen historical status asset.
    q5 = pd.read_csv(P1_Q5, dtype={"symbol": str, "signal_date": str, "entry_date": str, "exit_date": str})
    keys = q5[["symbol", "entry_date"]].drop_duplicates().copy()
    keys["entry_date"] = keys["entry_date"].astype(str).str.replace("-", "", regex=False).str[:8]
    import duckdb
    con = duckdb.connect(":memory:")
    con.register("keys", keys)
    pp = str(STATUS_PARQUET).replace("\\", "/").replace("'", "''")
    try:
        x = con.execute(f"""
            SELECT k.symbol, k.entry_date,
                   s.is_suspended, s.open, s.high, s.low, s.close, s.pre_close,
                   s.up_limit, s.can_buy_at_open, s.new_listing_special,
                   s.st_known, s.st_status
            FROM keys k
            LEFT JOIN read_parquet('{pp}') s
              ON s.symbol=k.symbol
             AND strftime(CAST(s.trade_date AS DATE),'%Y%m%d')=k.entry_date
        """).df()
    finally:
        con.unregister("keys")
        con.close()
    require(len(x) == len(keys), "status join row count mismatch")
    found = x["can_buy_at_open"].notna()
    require(bool(found.all()), "P1 Q5 eligible entry missing status")
    x["formula_can_buy"] = x.apply(historical_can_buy_formula, axis=1)
    mismatch = x[x["formula_can_buy"] != x["can_buy_at_open"].astype(bool)]
    # Candidate file already passed P1 hygiene, so special/new/ST-known rows should be absent.
    known_st = x["st_known"].fillna(False) & x["st_status"].fillna("").astype(str).str.upper().isin(["ST", "STAR_ST"])
    checks = {
        "historical_can_buy_formula_exact_on_p1_q5_entries": len(mismatch) == 0,
        "eligible_q5_entry_new_listing_special_zero": int(x["new_listing_special"].fillna(False).sum()) == 0,
        "eligible_q5_entry_known_st_zero": int(known_st.sum()) == 0,
        "eligible_q5_entry_suspended_zero": int(x["is_suspended"].fillna(False).sum()) == 0,
    }
    require(all(checks.values()), f"entry semantic equivalence failed checks={checks} mismatch={len(mismatch)}")
    return {
        "status": "PASS_ENTRY_FILLABILITY_EQUIVALENCE",
        "rows": int(len(x)),
        "formula_mismatch_rows": int(len(mismatch)),
        "new_listing_special_rows": int(x["new_listing_special"].fillna(False).sum()),
        "known_st_rows": int(known_st.sum()),
        "suspended_rows": int(x["is_suspended"].fillna(False).sum()),
        "checks": checks,
        "forward_mapping": resolved_forward_policy()["entry_fillability"],
    }


def prepare_forward_candidate_q5(p1):
    frozen, fa = p1.load_frozen_signal_events(SIGNAL_START, SIGNAL_CUTOFF)
    q5 = frozen[frozen["bucket"] == 5].copy()
    import duckdb
    con = duckdb.connect(str(CATALOG), read_only=True)
    try:
        calendar = p1.market_calendar(con, "20220101", LATEST)
        symbols = sorted(q5["symbol"].astype(str).unique().tolist())
        panel = p1.load_panel(con, symbols, LATEST)
        # capture raw suspend flag separately; p1 adjusted panel intentionally focuses economics.
        sy = pd.DataFrame({"symbol": symbols})
        con.register("p4b_symbols", sy)
        susp = con.execute("""
            SELECT CAST(b.symbol AS VARCHAR) symbol,
                   strftime(CAST(b.trade_date AS DATE),'%Y%m%d') trade_date,
                   CAST(b.suspend_flag AS INTEGER) suspend_flag,
                   CAST(b."close" AS DOUBLE) raw_close
            FROM market.daily_bars_v1 b
            INNER JOIN p4b_symbols s USING(symbol)
            WHERE strftime(CAST(b.trade_date AS DATE),'%Y%m%d') BETWEEN '20220101' AND ?
        """, [LATEST]).df()
        con.unregister("p4b_symbols")
    finally:
        con.close()

    timed = p1.add_timing(q5, calendar)
    sig = p1.load_status_for_keys(timed, "signal_date", "signal")
    ent = p1.load_status_for_keys(timed, "entry_date", "entry")
    x = timed.merge(sig, on=["symbol","signal_date"], how="left", validate="many_to_one")
    x = x.merge(ent, on=["symbol","entry_date"], how="left", validate="many_to_one")
    x["signal_known_st"] = x["signal_st_known"].fillna(False) & x["signal_st_status"].fillna("").astype(str).str.upper().isin(["ST","STAR_ST"])
    x["entry_known_st"] = x["entry_st_known"].fillna(False) & x["entry_st_status"].fillna("").astype(str).str.upper().isin(["ST","STAR_ST"])
    x["exclude_reason"] = ""
    rules = [
        (~x["signal_status_found"].fillna(False), "NO_SIGNAL_STATUS"),
        (x["signal_is_suspended"].fillna(False), "SIGNAL_SUSPENDED"),
        (x["signal_new_listing_special"].fillna(False), "SIGNAL_NEW_LISTING_SPECIAL"),
        (x["signal_known_st"], "SIGNAL_KNOWN_ST"),
        (~x["entry_status_found"].fillna(False), "NO_ENTRY_STATUS"),
        (x["entry_is_suspended"].fillna(False), "ENTRY_SUSPENDED"),
        (x["entry_new_listing_special"].fillna(False), "ENTRY_NEW_LISTING_SPECIAL"),
        (x["entry_known_st"], "ENTRY_KNOWN_ST"),
        (~x["entry_can_buy_at_open"].fillna(False), "ENTRY_NOT_BUYABLE_AT_OPEN"),
    ]
    for mask, reason in rules:
        x.loc[(x["exclude_reason"] == "") & mask, "exclude_reason"] = reason

    ep = panel[["symbol","trade_date","adj_open"]].rename(columns={"trade_date":"entry_date","adj_open":"entry_adj_open"})
    x = x.merge(ep, on=["symbol","entry_date"], how="left", validate="many_to_one")
    x.loc[(x["exclude_reason"] == "") & x["entry_adj_open"].isna(), "exclude_reason"] = "NO_EXACT_ENTRY_PRICE"
    eligible = x[x["exclude_reason"] == ""].copy()
    eligible = eligible.rename(columns={"exit_date":"planned_exit_date"})

    # executable CLOSE map: adjusted close only when the underlying daily row is not suspended
    # and has a real positive raw close. This avoids using a carried synthetic mark as an exit.
    pp = panel[["symbol","trade_date","adj_open","adj_close"]].merge(
        susp, on=["symbol","trade_date"], how="left", validate="one_to_one"
    )
    op, mark_close = p1.price_maps(panel)
    exec_close = {}
    for r in pp.itertuples(index=False):
        sf = getattr(r, "suspend_flag")
        raw = getattr(r, "raw_close")
        adj = getattr(r, "adj_close")
        ok_suspend = pd.isna(sf) or int(sf) == 0
        if ok_suspend and pd.notna(raw) and float(raw) > 0 and pd.notna(adj) and float(adj) > 0:
            exec_close[(str(r.symbol), str(r.trade_date))] = float(adj)
    return eligible, calendar, op, mark_close, exec_close, panel, fa


def simulate_deferred(p1, q5, calendar, open_map, mark_close_map, exec_close_map, round_trip_cost: float):
    by_entry = {str(d): g.to_dict("records") for d,g in q5.groupby("entry_date", sort=True)}
    half = round_trip_cost/2.0
    cash = p1.INITIAL_EQUITY
    active = {}
    last_close = {}
    trades, skipped, curve = [], [], []
    start = str(q5["entry_date"].min())
    dates = [d for d in calendar if start <= d <= LATEST]
    idx = {d:i for i,d in enumerate(calendar)}
    scaled_days = no_slot_days = 0

    for d in dates:
        open_mv = 0.0
        for sym,pos in active.items():
            px = open_map.get((sym,d), last_close.get(sym))
            require(px is not None and px > 0, f"no OPEN mark {sym} {d}")
            open_mv += pos["shares"]*px
        equity_open = cash + open_mv
        remaining_gross = max(0.0, p1.MAX_GROSS*equity_open-open_mv)
        remaining_cash = max(0.0, cash/(1.0+half))
        available = min(remaining_gross, remaining_cash)

        todays = list(by_entry.get(d, []))
        eligible=[]
        for r in todays:
            sym=str(r["symbol"])
            if sym in active:
                skipped.append({"symbol":sym,"signal_date":str(r["signal_date"]),"entry_date":d,"reason":"DUPLICATE_WHILE_HELD"})
            else:
                eligible.append(r)
        eligible.sort(key=lambda r:(str(r["symbol"]),str(r["signal_date"])))
        slots=max(0,p1.MAX_POSITIONS-len(active))
        selected=eligible[:slots]
        if eligible and slots==0: no_slot_days += 1
        for r in eligible[slots:]:
            skipped.append({"symbol":str(r["symbol"]),"signal_date":str(r["signal_date"]),"entry_date":d,"reason":"NO_SLOT"})
        desired_each=p1.TARGET_WEIGHT*equity_open
        desired_total=desired_each*len(selected)
        alloc_total=min(desired_total,available)
        scale=alloc_total/desired_total if desired_total>0 else 0.0
        if selected and scale<1.0-1e-12: scaled_days += 1
        if scale<=p1.EPS:
            for r in selected:
                skipped.append({"symbol":str(r["symbol"]),"signal_date":str(r["signal_date"]),"entry_date":d,"reason":"NO_GROSS_OR_CASH_CAPACITY"})
        else:
            notional=desired_each*scale
            for r in selected:
                sym=str(r["symbol"]); px=open_map.get((sym,d))
                require(px is not None and px>0, f"missing entry {sym} {d}")
                fee=notional*half
                require(notional+fee <= cash+1e-8, "cash invariant")
                shares=notional/px; cash-=notional+fee
                active[sym]={
                    "symbol":sym,"signal_date":str(r["signal_date"]),"entry_date":d,
                    "planned_exit_date":str(r["planned_exit_date"]),"ret120":float(r["ret120"]),
                    "entry_price":px,"entry_notional":notional,"entry_fee":fee,"shares":shares,
                }

        # Mark at close. A non-trading/suspended day uses last real mark, never as an execution price.
        for sym,pos in active.items():
            px=exec_close_map.get((sym,d), last_close.get(sym))
            require(px is not None and px>0, f"no CLOSE mark {sym} {d}")
            pos["close_mark"]=px
            if (sym,d) in exec_close_map: last_close[sym]=px

        for sym in list(active):
            pos=active[sym]
            planned=pos["planned_exit_date"]
            if idx.get(d,-1) < idx.get(planned,10**9):
                continue
            px=exec_close_map.get((sym,d))
            if px is None or px<=0:
                continue
            gross=pos["shares"]*px; exit_fee=gross*half
            cash += gross-exit_fee
            pnl=gross-exit_fee-pos["entry_notional"]-pos["entry_fee"]
            delay=idx[d]-idx[planned]
            trades.append({**pos,"actual_exit_date":d,"exit_price":px,"exit_delay_sessions":delay,
                           "deferred_exit":bool(delay>0),"net_pnl":pnl,
                           "net_return":pnl/pos["entry_notional"]})
            del active[sym]

        mv=0.0
        for sym,pos in active.items():
            px=exec_close_map.get((sym,d), last_close.get(sym))
            require(px is not None and px>0, f"no EOD mark {sym} {d}")
            mv += pos["shares"]*px
        equity=cash+mv
        curve.append({"date":d,"equity":equity,"cash":cash,"gross_market_value":mv,
                      "gross_weight":mv/equity if equity>0 else 0.0,"active_positions":len(active)})

    return {
        "curve": pd.DataFrame(curve),
        "trades": pd.DataFrame(trades),
        "skipped": pd.DataFrame(skipped),
        "active_end": pd.DataFrame(active.values()),
        "scaled_entry_days": scaled_days,
        "no_slot_days": no_slot_days,
    }


def audit_exit_policy(p1) -> dict[str, Any]:
    q5_forward, calendar, op, mark_cl, exec_cl, panel, frozen_audit = prepare_forward_candidate_q5(p1)
    exact_q5 = pd.read_csv(P1_Q5, dtype={"symbol":str,"signal_date":str,"entry_date":str,"exit_date":str})
    # P1 file uses exit_date; reconstruct baseline using the exact P1 simulator and current canonical panel.
    baseline = p1.simulate(exact_q5, calendar, op, mark_cl, p1.MAIN_COST, seed=None)
    baseline_metrics = p1.curve_metrics(baseline["curve"])
    p1s = read_json(P1_SUMMARY)
    expected = p1s["main_20bps"]["metrics"]
    for k,tol in (("cagr",1e-12),("max_drawdown",1e-12),("sharpe",1e-12)):
        require(abs(float(baseline_metrics[k])-float(expected[k])) <= tol, f"baseline reproduction {k} drift")

    d20 = simulate_deferred(p1,q5_forward,calendar,op,mark_cl,exec_cl,p1.MAIN_COST)
    d50 = simulate_deferred(p1,q5_forward,calendar,op,mark_cl,exec_cl,p1.STRESS_COST)
    m20=p1.curve_metrics(d20["curve"]); m50=p1.curve_metrics(d50["curve"])
    tr=d20["trades"]
    delayed=int(tr["deferred_exit"].sum()) if len(tr) else 0
    frac=delayed/len(tr) if len(tr) else 0.0
    delays=tr.loc[tr.get("deferred_exit",False)==True,"exit_delay_sessions"] if len(tr) else pd.Series(dtype=float)
    active_end=d20["active_end"]
    due_unresolved=0
    if len(active_end):
        due_unresolved=int((active_end["planned_exit_date"].astype(str) <= LATEST).sum())

    dc=float(m20["cagr"])-float(expected["cagr"])
    dm=float(m20["max_drawdown"])-float(expected["max_drawdown"])
    ds=float(m20["sharpe"])-float(expected["sharpe"])
    checks={
        "baseline_exact_reproduced": True,
        "delayed_executed_fraction_le_5pct": frac <= MAX_DELAYED_EXECUTED_FRACTION,
        "abs_cagr_drift_le_50bp": abs(dc) <= MAX_ABS_CAGR_DRIFT,
        "abs_mdd_drift_le_50bp": abs(dm) <= MAX_ABS_MDD_DRIFT,
        "abs_sharpe_drift_le_0_10": abs(ds) <= MAX_ABS_SHARPE_DRIFT,
        "no_exit_before_s120": bool((tr["exit_delay_sessions"] >= 0).all()) if len(tr) else True,
    }
    status="PASS_S120_DEFER_TO_NEXT_TRADABLE_CLOSE_ROBUSTNESS" if all(checks.values()) else "FAIL_S120_EXIT_POLICY_ROBUSTNESS"
    return {
        "status":status,
        "predeclared_policy":resolved_forward_policy()["exit"],
        "predeclared_guardrails":{
            "max_delayed_executed_fraction":MAX_DELAYED_EXECUTED_FRACTION,
            "max_abs_cagr_drift":MAX_ABS_CAGR_DRIFT,
            "max_abs_mdd_drift":MAX_ABS_MDD_DRIFT,
            "max_abs_sharpe_drift":MAX_ABS_SHARPE_DRIFT,
        },
        "candidate_rows_exact_exit_cohort":int(len(exact_q5)),
        "candidate_rows_forward_policy":int(len(q5_forward)),
        "main_20bps_baseline_exact":baseline_metrics,
        "main_20bps_deferred_policy":m20,
        "stress_50bps_deferred_policy":m50,
        "delta_deferred_minus_exact":{"cagr":dc,"max_drawdown":dm,"sharpe":ds},
        "executed_trades":int(len(tr)),
        "delayed_exit_trades":delayed,
        "delayed_exit_fraction":frac,
        "delay_sessions":{"max":int(delays.max()) if len(delays) else 0,
                          "median":float(delays.median()) if len(delays) else 0.0,
                          "p90":float(delays.quantile(.9)) if len(delays) else 0.0},
        "due_but_unresolved_positions_at_20260930":due_unresolved,
        "active_positions_at_20260930":int(len(active_end)),
        "checks":checks,
    }, d20, d50


def check() -> int:
    fp=verify_static_inputs()
    result={
        "status":"PASS_RET120_FORWARD_SEMANTICS_STATIC_CHECK_V0_1",
        "strategy2_qualification":EXPECTED_QUAL,
        "blockers_to_resolve":["ENTRY_FILLABILITY_EQUIVALENCE","S120_EXIT_SUSPENSION_POLICY","FORWARD_REVIEW_GATE"],
        "predeclared_forward_policy":resolved_forward_policy(),
        "exit_robustness_guardrails":{
            "max_delayed_executed_fraction":MAX_DELAYED_EXECUTED_FRACTION,
            "max_abs_cagr_drift":MAX_ABS_CAGR_DRIFT,
            "max_abs_mdd_drift":MAX_ABS_MDD_DRIFT,
            "max_abs_sharpe_drift":MAX_ABS_SHARPE_DRIFT,
        },
        "fingerprints":fp,
        "no_qmt":True,"no_network":True,"no_broker":True,"no_database_mutation":True,
    }
    print(json.dumps(result,ensure_ascii=False,indent=2)); return 0


def run() -> int:
    fp=verify_static_inputs()
    p1=load_module(P1_SCRIPT,"px_ret120_p1")
    # P1 re-verifies its own frozen lineage/fingerprints.
    p1_fp=p1.verify_inputs()
    entry=audit_historical_can_buy_equivalence(p1)
    exit_audit,d20,d50=audit_exit_policy(p1)
    require(exit_audit["status"].startswith("PASS_"), f"exit policy audit status={exit_audit['status']}")
    policy=resolved_forward_policy()
    result={
        "status":PASS_STATUS,
        "generated_at":pd.Timestamp.now(tz="Asia/Singapore").isoformat(),
        "strategy2_status":EXPECTED_QUAL,
        "resolved_blockers":{
            "ENTRY_FILLABILITY_EQUIVALENCE":entry,
            "S120_EXIT_SUSPENSION_POLICY":exit_audit,
            "FORWARD_REVIEW_GATE":{"status":"FROZEN_RET120_REVIEW_GATE_V0_1",**review_gate()},
        },
        "forward_protocol_status":"READY_FOR_FORWARD_RUNNER_IMPLEMENTATION_NOT_STARTED",
        "frozen_forward_policy":policy,
        "important_boundaries":[
            "Strategy #2 historical qualification is already frozen and is not re-optimized here.",
            "The S120 deferral rule was declared before this audit result; failure must be investigated, not replaced by an outcome-selected alternative.",
            "Forward collection remains research-only. No broker, production orders, or scheduler are created by this audit.",
            "The 12-month/60-trade review gate is a qualification review gate, not a prohibition on earlier operational health checks."
        ],
        "fingerprints":{**fp,"p1_runtime":p1_fp},
        "no_qmt":True,"no_network":True,"no_broker":True,"no_production_db":True,"no_database_mutation":True,
    }
    OUT_DIR.mkdir(parents=True,exist_ok=True)
    write_json_atomic(OUT_DIR/"ret120_forward_execution_semantics_v01.json",result)
    d20["curve"].to_csv(OUT_DIR/"deferred_exit_equity_curve_20bps.csv",index=False)
    d20["trades"].to_csv(OUT_DIR/"deferred_exit_trades_20bps.csv",index=False)
    d20["active_end"].to_csv(OUT_DIR/"deferred_exit_active_end_20bps.csv",index=False)
    d50["curve"].to_csv(OUT_DIR/"deferred_exit_equity_curve_50bps.csv",index=False)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print(f"OUTPUT: {OUT_DIR}")
    return 0


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=["check","run"],nargs="?",default="check")
    args=ap.parse_args()
    return check() if args.command=="check" else run()

if __name__=="__main__":
    raise SystemExit(main())
