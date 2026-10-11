from __future__ import annotations

r"""
PositionX — ALPHA2_INCREMENTAL_VALUE_V0.1.3

Lineage-fixed version. Requires the Deep Oversold V0.1.1 frozen-event replay to
PASS before doing any diversification comparison.
"""

import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(r"D:\positionx_data\research\alpha2_incremental_value_v0_1")
DEEP_ROOT = ROOT / "deep_oversold_export"
DEEP_SUMMARY = DEEP_ROOT / "deep_oversold_export_summary_v013.json"
DEEP_CURVE = DEEP_ROOT / "deep_oversold_20bps_curve.csv"
DEEP_TRADES = DEEP_ROOT / "deep_oversold_20bps_trades.csv"
DEEP_FROZEN_EVENTS = DEEP_ROOT / "deep_oversold_frozen_dedup_events.csv"
DEEP_CURRENT_CURVE = DEEP_ROOT / "deep_oversold_current_regenerated_20bps_curve.csv"

EARN_ROOT = Path(r"D:\positionx_data\research\earnings_improving_h60_portfolio_v0_1")
EARN_SUMMARY = EARN_ROOT / "portfolio_summary.json"
EARN_CURVE = EARN_ROOT / "equity_curve_20bps.csv"
EARN_TRADES = EARN_ROOT / "trades_20bps.csv"

EXPECTED_EARN_CURVE_SHA256 = "8389e7a3cc77ed91cc3ff0c0c96b46f96e7566f48bf539937a8aef1766cd283f"
EXPECTED_EARN_SUMMARY_SHA256 = "ae24ebf363cb74aca40f0fecf00f3f83afc36bc377a60017f7725787d1d208f8"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def write_json_atomic(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def parse_date_series(s: pd.Series) -> pd.Series:
    # Handles YYYYMMDD integers/strings and ISO dates safely.
    raw = s.astype(str).str.replace(".0", "", regex=False)
    eight = raw.str.fullmatch(r"\d{8}")
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    if eight.any():
        out.loc[eight] = pd.to_datetime(raw.loc[eight], format="%Y%m%d", errors="coerce")
    if (~eight).any():
        out.loc[~eight] = pd.to_datetime(raw.loc[~eight], errors="coerce")
    return out


def normalize_curve(path: Path, label: str) -> pd.DataFrame:
    x = pd.read_csv(path)
    date_col = next((c for c in ("date","trade_date","session") if c in x.columns), None)
    eq_col = next((c for c in ("equity","nav","base_equity") if c in x.columns), None)
    gross_col = next((c for c in ("gross_weight","gross_exposure","avg_gross_weight","gross") if c in x.columns), None)
    active_col = next((c for c in ("active_positions","unique_positions","positions","active") if c in x.columns), None)
    if date_col is None or eq_col is None:
        raise RuntimeError(f"FAIL_CLOSED {label} curve columns={list(x.columns)}")

    out = pd.DataFrame({
        "date": parse_date_series(x[date_col]),
        f"{label}_equity": pd.to_numeric(x[eq_col], errors="coerce"),
    })
    out[f"{label}_gross"] = (
        pd.to_numeric(x[gross_col], errors="coerce") if gross_col else np.nan
    )
    out[f"{label}_active"] = (
        pd.to_numeric(x[active_col], errors="coerce") if active_col else np.nan
    )
    if "daily_return" in x.columns:
        out[f"{label}_return"] = pd.to_numeric(x["daily_return"], errors="coerce")
    else:
        out[f"{label}_return"] = out[f"{label}_equity"].pct_change()

    return (
        out.dropna(subset=["date",f"{label}_equity"])
        .sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )


def metric(dates, ret):
    z = pd.DataFrame({
        "date": pd.to_datetime(dates),
        "r": pd.to_numeric(ret, errors="coerce"),
    }).dropna()
    nav = (1.0 + z["r"]).cumprod()
    years = max((z["date"].iloc[-1]-z["date"].iloc[0]).days,1)/365.25
    cagr = float(nav.iloc[-1]**(1/years)-1)
    dd = nav/nav.cummax()-1
    sd = float(z["r"].std(ddof=1))
    ann = len(z)/years
    sharpe = float(z["r"].mean()/sd*np.sqrt(ann)) if sd>0 else None
    return {
        "sessions": int(len(z)),
        "start": str(z["date"].iloc[0].date()),
        "end": str(z["date"].iloc[-1].date()),
        "total_return": float(nav.iloc[-1]-1),
        "cagr": cagr,
        "max_drawdown": float(dd.min()),
        "sharpe": sharpe,
    }


def normalize_trades(path: Path) -> pd.DataFrame:
    x = pd.read_csv(path)
    if "symbol" not in x.columns:
        return pd.DataFrame(columns=["symbol","entry_date","exit_date"])
    ec = next((c for c in ("entry_date","buy_date") if c in x.columns), None)
    xc = next((c for c in ("exit_date","sell_date") if c in x.columns), None)
    if ec is None or xc is None:
        return pd.DataFrame(columns=["symbol","entry_date","exit_date"])
    return pd.DataFrame({
        "symbol": x["symbol"].astype(str),
        "entry_date": parse_date_series(x[ec]),
        "exit_date": parse_date_series(x[xc]),
    }).dropna()


def holding_overlap(a: pd.DataFrame, b: pd.DataFrame, dates) -> tuple[dict,pd.DataFrame]:
    rows=[]
    for d in pd.to_datetime(dates):
        sa=set(a.loc[(a.entry_date<=d)&(a.exit_date>=d),"symbol"])
        sb=set(b.loc[(b.entry_date<=d)&(b.exit_date>=d),"symbol"])
        inter=sa&sb
        union=sa|sb
        rows.append({
            "date":d,
            "deep_active_symbols":len(sa),
            "earn_active_symbols":len(sb),
            "same_symbol_active":len(inter),
            "symbol_jaccard":len(inter)/len(union) if union else 0.0,
        })
    x=pd.DataFrame(rows)
    return {
        "days_with_same_symbol_active":int((x.same_symbol_active>0).sum()),
        "fraction_days_with_same_symbol_active":float((x.same_symbol_active>0).mean()),
        "mean_same_symbol_active":float(x.same_symbol_active.mean()),
        "max_same_symbol_active":int(x.same_symbol_active.max()),
        "mean_symbol_jaccard":float(x.symbol_jaccard.mean()),
    },x


def main() -> int:
    for p in (DEEP_SUMMARY,DEEP_CURVE,EARN_SUMMARY,EARN_CURVE):
        if not p.exists():
            raise RuntimeError(f"FAIL_CLOSED missing: {p}")

    ds=json.loads(DEEP_SUMMARY.read_text(encoding="utf-8"))
    allowed_deep_status = {
        "PASS_DEEP_OVERSOLD_EXACT_REPLAY_V0_1_3",
        "PASS_DEEP_OVERSOLD_ECONOMIC_EQUIVALENCE_V0_1_3",
    }
    if ds.get("status") not in allowed_deep_status:
        raise RuntimeError(
            f"FAIL_CLOSED Deep replay is not approved for Alpha2: {ds.get('status')}"
        )
    es=json.loads(EARN_SUMMARY.read_text(encoding="utf-8"))
    if sha256_file(EARN_SUMMARY)!=EXPECTED_EARN_SUMMARY_SHA256:
        raise RuntimeError("FAIL_CLOSED earnings summary hash drift")
    if sha256_file(EARN_CURVE)!=EXPECTED_EARN_CURVE_SHA256:
        raise RuntimeError("FAIL_CLOSED earnings curve hash drift")
    if es.get("standalone_gate",{}).get("status")!="PASS_STANDALONE_PORTFOLIO_GATE":
        raise RuntimeError("FAIL_CLOSED earnings standalone gate not PASS")

    d=normalize_curve(DEEP_CURVE,"deep")
    e=normalize_curve(EARN_CURVE,"earn")
    c=d.merge(e,on="date",how="inner").sort_values("date").reset_index(drop=True)
    if len(c)<500:
        raise RuntimeError(f"FAIL_CLOSED common sessions={len(c)}")

    c["deep_return"]=pd.to_numeric(c.deep_return,errors="coerce").fillna(0)
    c["earn_return"]=pd.to_numeric(c.earn_return,errors="coerce").fillna(0)
    c["deep_nav"]=(1+c.deep_return).cumprod()
    c["earn_nav"]=(1+c.earn_return).cumprod()

    # Fixed 50/50 initial-capital sleeves; no weight optimization/rebalancing.
    c["combo_nav"]=0.5*c.deep_nav+0.5*c.earn_nav
    c["combo_return"]=c.combo_nav.pct_change().fillna(c.combo_nav.iloc[0]-1)

    dm=metric(c.date,c.deep_return)
    em=metric(c.date,c.earn_return)
    cm=metric(c.date,c.combo_return)

    sensitivity = None
    if DEEP_CURRENT_CURVE.exists():
        dc = normalize_curve(DEEP_CURRENT_CURVE, "deep_current")
        sc = dc.merge(e, on="date", how="inner").sort_values("date").reset_index(drop=True)
        sc["deep_current_return"] = pd.to_numeric(
            sc["deep_current_return"], errors="coerce"
        ).fillna(0)
        sc["earn_return"] = pd.to_numeric(sc["earn_return"], errors="coerce").fillna(0)
        sc["deep_current_nav"] = (1 + sc["deep_current_return"]).cumprod()
        sc["earn_nav"] = (1 + sc["earn_return"]).cumprod()
        sc["combo_nav"] = 0.5 * sc["deep_current_nav"] + 0.5 * sc["earn_nav"]
        sc["combo_return"] = sc["combo_nav"].pct_change().fillna(sc["combo_nav"].iloc[0]-1)

        sdm = metric(sc.date, sc.deep_current_return)
        scm = metric(sc.date, sc.combo_return)
        sensitivity = {
            "deep_current_regenerated": sdm,
            "combo_with_current_regenerated": scm,
            "daily_return_correlation_with_earnings": float(
                sc.deep_current_return.corr(sc.earn_return)
            ),
            "delta_combo_vs_frozen_event_replay_combo": {
                "cagr": scm["cagr"] - cm["cagr"],
                "max_drawdown": scm["max_drawdown"] - cm["max_drawdown"],
                "sharpe": scm["sharpe"] - cm["sharpe"],
            },
        }
        sc.to_csv(ROOT/"common_daily_current_regenerated_v013.csv",index=False,encoding="utf-8-sig")

    c["deep_dd"]=c.deep_nav/c.deep_nav.cummax()-1
    c["earn_dd"]=c.earn_nav/c.earn_nav.cummax()-1
    c["combo_dd"]=c.combo_nav/c.combo_nav.cummax()-1

    c["combo_gross"]=(
        0.5*c.deep_nav*c.deep_gross.fillna(0)
        +0.5*c.earn_nav*c.earn_gross.fillna(0)
    )/c.combo_nav
    c["full_overlay_gross"]=c.deep_gross.fillna(0)+c.earn_gross.fillna(0)

    both_active=(c.deep_gross.fillna(0)>0.005)&(c.earn_gross.fillna(0)>0.005)
    both_under=(c.deep_dd<0)&(c.earn_dd<0)
    both_dd2=(c.deep_dd<=-0.02)&(c.earn_dd<=-0.02)

    hsum={}
    hdaily=pd.DataFrame()
    holding_overlap_basis = None
    if EARN_TRADES.exists():
        et=normalize_trades(EARN_TRADES)
        dt=pd.DataFrame()
        if DEEP_TRADES.exists():
            dt=normalize_trades(DEEP_TRADES)
            if len(dt):
                holding_overlap_basis = "EXACT_DEEP_EXECUTED_TRADES"
        if dt.empty and DEEP_FROZEN_EVENTS.exists():
            # Frozen simulator exposes only executed-trade COUNT, not row-level
            # executed trades. Use all 1468 dedup event holding intervals as a
            # conservative upper-bound proxy. This can only overstate overlap
            # because the portfolio executed 1365 of those events.
            dt=normalize_trades(DEEP_FROZEN_EVENTS)
            if len(dt):
                holding_overlap_basis = "FROZEN_DEDUP_EVENTS_UPPER_BOUND"
        if len(dt) and len(et):
            hsum,hdaily=holding_overlap(dt,et,c.date)
            hsum["basis"] = holding_overlap_basis
            hsum["interpretation"] = (
                "UPPER_BOUND: Deep side includes all frozen dedup events, including "
                "capacity-skipped events."
                if holding_overlap_basis == "FROZEN_DEDUP_EVENTS_UPPER_BOUND"
                else "Exact executed-trade holding overlap."
            )

    ROOT.mkdir(parents=True,exist_ok=True)
    c.to_csv(ROOT/"common_daily_v013.csv",index=False,encoding="utf-8-sig")
    if len(hdaily):
        hdaily.to_csv(ROOT/"holding_overlap_daily_v013.csv",index=False,encoding="utf-8-sig")

    out={
        "status":"PASS_ALPHA2_INCREMENTAL_VALUE_AUDIT_V0_1_3",
        "generated_at":datetime.now().astimezone().isoformat(timespec="seconds"),
        "common_period":{
            "start":str(c.date.min().date()),
            "end":str(c.date.max().date()),
            "sessions":int(len(c)),
        },
        "metrics":{
            "deep_oversold":dm,
            "earnings_improving_h60":em,
            "fixed_50_50_sleeves":cm,
        },
        "delta_fixed_50_50":{
            "vs_deep":{
                "cagr":cm["cagr"]-dm["cagr"],
                "max_drawdown":cm["max_drawdown"]-dm["max_drawdown"],
                "sharpe":cm["sharpe"]-dm["sharpe"],
            },
            "vs_earnings":{
                "cagr":cm["cagr"]-em["cagr"],
                "max_drawdown":cm["max_drawdown"]-em["max_drawdown"],
                "sharpe":cm["sharpe"]-em["sharpe"],
            },
        },
        "correlation_and_drawdown":{
            "daily_return_correlation":float(c.deep_return.corr(c.earn_return)),
            "drawdown_correlation":float(c.deep_dd.corr(c.earn_dd)),
            "both_underwater_fraction":float(both_under.mean()),
            "both_drawdown_le_2pct_fraction":float(both_dd2.mean()),
        },
        "capital_overlap":{
            "both_active_fraction":float(both_active.mean()),
            "mean_min_gross":float(np.minimum(c.deep_gross.fillna(0),c.earn_gross.fillna(0)).mean()),
            "deep_mean_gross":float(c.deep_gross.mean()),
            "earn_mean_gross":float(c.earn_gross.mean()),
            "fixed_50_50_mean_gross":float(c.combo_gross.mean()),
            "fixed_50_50_max_gross":float(c.combo_gross.max()),
            "full_overlay_mean_gross_diagnostic":float(c.full_overlay_gross.mean()),
            "full_overlay_p95_gross_diagnostic":float(c.full_overlay_gross.quantile(.95)),
            "full_overlay_max_gross_diagnostic":float(c.full_overlay_gross.max()),
        },
        "holding_overlap":hsum,
        "deep_replay_lineage_status": ds.get("status"),
        "deep_market_input_drift": ds.get("market_history_drift"),
        "signal_lineage_sensitivity": sensitivity,
        "guardrail":(
            "Fixed 50/50 sleeves are a non-optimized diversification diagnostic, "
            "not the final Strategy Portfolio engine."
        ),
        "no_network":True,
        "no_database_mutation":True,
    }
    write_json_atomic(ROOT/"incremental_value_summary_v013.json",out)
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
