from __future__ import annotations

r"""
PositionX — RET120 vs Deep Oversold Incremental Value Audit V0.1

Purpose
-------
P2: freeze RET120_H120 as the representative of the Trend/underreaction family
    after the already-completed comparable 52WH family audit.
P3: compare that survivor with frozen Deep Oversold Strategy #1 on a common
    historical period using a fixed 50/50 initial-capital sleeve diagnostic.

Guardrails
----------
* No signal tuning.
* No weight optimization.
* No daily sleeve rebalancing.
* Deep frozen/economic-equivalence lineage is not rewritten.
* ret120 exact frozen Q5 membership is consumed from the P1 emitted candidate
  events; it is NOT recomputed from current prices.
* 100 ret120 same-day random tie-breaks are replayed so diversification evidence
  is not allowed to depend only on SYMBOL_ASC.
* This audit does NOT automatically grant Strategy #2 status. It emits evidence
  for the subsequent P4 qualification decision.
"""

import argparse
import hashlib
import importlib.util
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
P1_SCRIPT = PROJECT_ROOT / "scripts" / "audit_ret120_frozen_signal_comparable_v02.py"
EXPECTED_P1_SCRIPT_SHA256 = "38c5d1dbb8c1e4c5bde16b6bf3e0ca0b62b783942ea61f83a771037e48061804"

RET_ROOT = Path(r"D:\positionx_data\research\ret120_vs_52wh_family_v0_2_frozen_signal")
RET_SUMMARY = RET_ROOT / "ret120_comparable_summary.json"
RET_FAMILY = RET_ROOT / "family_comparison_summary.json"
RET_CURVE = RET_ROOT / "ret120_equity_curve_20bps.csv"
RET_TRADES = RET_ROOT / "ret120_trades_20bps.csv"
RET_Q5 = RET_ROOT / "ret120_q5_candidate_events.csv"

DEEP_ROOT = Path(r"D:\positionx_data\research\alpha2_incremental_value_v0_1\deep_oversold_export")
DEEP_SUMMARY = DEEP_ROOT / "deep_oversold_export_summary_v013.json"
DEEP_CURVE = DEEP_ROOT / "deep_oversold_20bps_curve.csv"
DEEP_TRADES = DEEP_ROOT / "deep_oversold_20bps_trades.csv"
DEEP_FROZEN_EVENTS = DEEP_ROOT / "deep_oversold_frozen_dedup_events.csv"

OUT_ROOT = Path(r"D:\positionx_data\research\ret120_vs_deep_incremental_v0_1")

EXPECTED_RET_STATUS = "PASS_RET120_FROZEN_SIGNAL_COMPARABLE_PORTFOLIO_AUDIT_V0_2"
EXPECTED_FAMILY_STATUS = "PASS_RET120_VS_52WH_FAMILY_COMPARISON_V0_2_FROZEN_SIGNAL"
EXPECTED_DEEP_STATUSES = {
    "PASS_DEEP_OVERSOLD_EXACT_REPLAY_V0_1_3",
    "PASS_DEEP_OVERSOLD_ECONOMIC_EQUIVALENCE_V0_1_3",
}
EXPECTED_PANEL_SHA = "66b358a54c33866dd03aae187c1d60379a0317df3c15fe3d453babb430b338f1"
EXPECTED_BUCKET_SHA = "936b6c1d70314958b6a10fec317b8ab1938cfff6ab02390a7c8cff798458f4ae"
EXPECTED_DEEP_RUNNER_SHA = "e18792cb9f3676bc515d937fe555cab7d28e4f2e94f918385bace4f54e51e73c"
EXPECTED_DEEP_DEDUP_SHA = "6c681bd0c943195b728058902549091e5f4c6adf66562352505d4c6ef40f50f0"
EXPECTED_DEEP_FROZEN_ROWS = 1468
EXPECTED_DEEP_FROZEN_TRADES = 1365
EXPECTED_DEEP_FROZEN_CAGR = 0.11039928725435111
EXPECTED_DEEP_FROZEN_MDD = -0.09624091257275003
EXPECTED_DEEP_FROZEN_SHARPE = 1.0418115217631059
EXPECTED_Q5_ROWS = 54058
EXPECTED_RET_TRADES = 150
EPS = 1e-12


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


def load_json(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"FAIL_CLOSED missing {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def require_close(label: str, actual, expected, tol=1e-10):
    if actual is None or not math.isfinite(float(actual)) or abs(float(actual)-float(expected)) > tol:
        raise RuntimeError(f"FAIL_CLOSED {label} expected={expected} actual={actual}")


def load_p1_module():
    if not P1_SCRIPT.exists():
        raise RuntimeError(f"FAIL_CLOSED missing P1 script {P1_SCRIPT}")
    actual = sha256_file(P1_SCRIPT)
    if actual != EXPECTED_P1_SCRIPT_SHA256:
        raise RuntimeError(
            f"FAIL_CLOSED P1 script hash drift expected={EXPECTED_P1_SCRIPT_SHA256} actual={actual}"
        )
    spec = importlib.util.spec_from_file_location("px_ret120_p1", str(P1_SCRIPT))
    if spec is None or spec.loader is None:
        raise RuntimeError("FAIL_CLOSED cannot import P1 script")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def normalize_date(s: pd.Series) -> pd.Series:
    raw = s.astype(str).str.replace(".0", "", regex=False)
    eight = raw.str.fullmatch(r"\d{8}")
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    if eight.any():
        out.loc[eight] = pd.to_datetime(raw.loc[eight], format="%Y%m%d", errors="coerce")
    if (~eight).any():
        out.loc[~eight] = pd.to_datetime(raw.loc[~eight], errors="coerce")
    return out


def normalize_curve(path: Path, label: str, initial_equity: float = 1_000_000.0) -> pd.DataFrame:
    if not path.exists():
        raise RuntimeError(f"FAIL_CLOSED missing curve {path}")
    x = pd.read_csv(path)
    date_col = next((c for c in ("date","trade_date","session") if c in x.columns), None)
    eq_col = next((c for c in ("equity","nav","base_equity") if c in x.columns), None)
    gross_col = next((c for c in ("gross_weight","gross_exposure","avg_gross_weight","gross") if c in x.columns), None)
    active_col = next((c for c in ("active_positions","unique_positions","positions","active") if c in x.columns), None)
    if date_col is None or eq_col is None:
        raise RuntimeError(f"FAIL_CLOSED {label} curve columns={list(x.columns)}")
    out = pd.DataFrame({
        "date": normalize_date(x[date_col]),
        f"{label}_equity": pd.to_numeric(x[eq_col], errors="coerce"),
    })
    out[f"{label}_gross"] = pd.to_numeric(x[gross_col], errors="coerce") if gross_col else np.nan
    out[f"{label}_active"] = pd.to_numeric(x[active_col], errors="coerce") if active_col else np.nan
    if "daily_return" in x.columns:
        out[f"{label}_return"] = pd.to_numeric(x["daily_return"], errors="coerce")
    else:
        out[f"{label}_return"] = out[f"{label}_equity"].pct_change(fill_method=None)
        if len(out) and pd.notna(out[f"{label}_equity"].iloc[0]):
            out.loc[out.index[0], f"{label}_return"] = float(out[f"{label}_equity"].iloc[0]) / initial_equity - 1.0
    return out.dropna(subset=["date",f"{label}_equity"]).sort_values("date").drop_duplicates("date",keep="last").reset_index(drop=True)


def metric_from_returns(dates, ret) -> dict:
    z = pd.DataFrame({"date": pd.to_datetime(dates), "r": pd.to_numeric(ret, errors="coerce")}).dropna()
    if len(z) < 2:
        raise RuntimeError("FAIL_CLOSED metric series too short")
    nav = (1.0 + z["r"]).cumprod()
    years = max((z["date"].iloc[-1] - z["date"].iloc[0]).days, 1) / 365.2425
    cagr = float(nav.iloc[-1] ** (1.0 / years) - 1.0)
    dd = nav / nav.cummax() - 1.0
    sd = float(z["r"].std(ddof=1))
    sharpe = float(z["r"].mean() / sd * math.sqrt(252.0)) if sd > 0 else None
    return {
        "sessions": int(len(z)),
        "start": str(z["date"].iloc[0].date()),
        "end": str(z["date"].iloc[-1].date()),
        "total_return": float(nav.iloc[-1]-1.0),
        "cagr": cagr,
        "max_drawdown": float(dd.min()),
        "sharpe": sharpe,
    }


def fixed_50_50(deep: pd.DataFrame, trend: pd.DataFrame) -> tuple[pd.DataFrame, dict, dict, dict]:
    c = deep.merge(trend, on="date", how="inner").sort_values("date").reset_index(drop=True)
    if len(c) < 500:
        raise RuntimeError(f"FAIL_CLOSED common sessions={len(c)}")
    c["deep_return"] = pd.to_numeric(c["deep_return"], errors="coerce").fillna(0.0)
    c["trend_return"] = pd.to_numeric(c["trend_return"], errors="coerce").fillna(0.0)
    c["deep_nav"] = (1.0 + c["deep_return"]).cumprod()
    c["trend_nav"] = (1.0 + c["trend_return"]).cumprod()
    c["combo_nav"] = 0.5*c["deep_nav"] + 0.5*c["trend_nav"]
    c["combo_return"] = c["combo_nav"].pct_change(fill_method=None)
    c.loc[c.index[0], "combo_return"] = c.loc[c.index[0], "combo_nav"] - 1.0
    c["deep_dd"] = c["deep_nav"] / c["deep_nav"].cummax() - 1.0
    c["trend_dd"] = c["trend_nav"] / c["trend_nav"].cummax() - 1.0
    c["combo_dd"] = c["combo_nav"] / c["combo_nav"].cummax() - 1.0
    dg = pd.to_numeric(c.get("deep_gross", 0.0), errors="coerce").fillna(0.0)
    tg = pd.to_numeric(c.get("trend_gross", 0.0), errors="coerce").fillna(0.0)
    c["combo_gross"] = (0.5*c.deep_nav*dg + 0.5*c.trend_nav*tg) / c.combo_nav
    c["full_overlay_gross"] = dg + tg
    dm = metric_from_returns(c.date, c.deep_return)
    tm = metric_from_returns(c.date, c.trend_return)
    cm = metric_from_returns(c.date, c.combo_return)
    return c, dm, tm, cm


def normalize_trades(path: Path, label: str) -> pd.DataFrame:
    if not path.exists():
        raise RuntimeError(f"FAIL_CLOSED missing trades {path}")
    x = pd.read_csv(path, dtype={"symbol": str})
    if "symbol" not in x.columns:
        raise RuntimeError(f"FAIL_CLOSED {label} trades missing symbol")
    ec = next((c for c in ("entry_date","buy_date") if c in x.columns), None)
    xc = next((c for c in ("exit_date","sell_date") if c in x.columns), None)
    if ec is None or xc is None:
        raise RuntimeError(f"FAIL_CLOSED {label} trades missing entry/exit date")
    out = pd.DataFrame({
        "symbol": x["symbol"].astype(str),
        "entry_date": normalize_date(x[ec]),
        "exit_date": normalize_date(x[xc]),
    })
    if "signal_date" in x.columns:
        out["signal_date"] = normalize_date(x["signal_date"])
    return out.dropna(subset=["symbol","entry_date","exit_date"]).reset_index(drop=True)


def normalize_signal_events(path: Path, label: str) -> pd.DataFrame:
    if not path.exists():
        raise RuntimeError(f"FAIL_CLOSED missing signal events {path}")
    x = pd.read_csv(path, dtype={"symbol": str})
    if "symbol" not in x.columns or "signal_date" not in x.columns:
        raise RuntimeError(f"FAIL_CLOSED {label} signal columns={list(x.columns)}")
    out = pd.DataFrame({"symbol": x["symbol"].astype(str), "signal_date": normalize_date(x["signal_date"])})
    return out.dropna().drop_duplicates().reset_index(drop=True)


def key_overlap(a: pd.DataFrame, b: pd.DataFrame, cols: list[str]) -> dict:
    A=set(map(tuple,a[cols].astype(str).to_numpy())) if len(a) else set()
    B=set(map(tuple,b[cols].astype(str).to_numpy())) if len(b) else set()
    inter=len(A&B); union=len(A|B)
    return {"a_keys":len(A),"b_keys":len(B),"intersection":inter,"jaccard":float(inter/union) if union else 0.0}


def holding_overlap(a: pd.DataFrame, b: pd.DataFrame, dates) -> tuple[dict,pd.DataFrame]:
    rows=[]
    for d in pd.to_datetime(dates):
        sa=set(a.loc[(a.entry_date<=d)&(a.exit_date>=d),"symbol"])
        sb=set(b.loc[(b.entry_date<=d)&(b.exit_date>=d),"symbol"])
        inter=sa&sb; union=sa|sb
        rows.append({"date":d,"deep_active_symbols":len(sa),"trend_active_symbols":len(sb),"same_symbol_active":len(inter),"symbol_jaccard":len(inter)/len(union) if union else 0.0})
    x=pd.DataFrame(rows)
    return {
        "days_with_same_symbol_active":int((x.same_symbol_active>0).sum()),
        "fraction_days_with_same_symbol_active":float((x.same_symbol_active>0).mean()),
        "mean_same_symbol_active":float(x.same_symbol_active.mean()),
        "max_same_symbol_active":int(x.same_symbol_active.max()),
        "mean_symbol_jaccard":float(x.symbol_jaccard.mean()),
    },x


def qtile(s: pd.Series) -> dict:
    x=pd.to_numeric(s,errors="coerce").dropna()
    return {k:float(v) for k,v in {
        "min":x.min(),"p10":x.quantile(.10),"p25":x.quantile(.25),"median":x.median(),
        "p75":x.quantile(.75),"p90":x.quantile(.90),"max":x.max(),"mean":x.mean()
    }.items()}


def verify_ret120() -> tuple[dict,dict]:
    for p in (RET_SUMMARY,RET_FAMILY,RET_CURVE,RET_TRADES,RET_Q5):
        if not p.exists(): raise RuntimeError(f"FAIL_CLOSED missing P1 artifact {p}")
    s=load_json(RET_SUMMARY); f=load_json(RET_FAMILY)
    if s.get("status") != EXPECTED_RET_STATUS:
        raise RuntimeError(f"FAIL_CLOSED ret120 status={s.get('status')}")
    if f.get("status") != EXPECTED_FAMILY_STATUS:
        raise RuntimeError(f"FAIL_CLOSED family status={f.get('status')}")
    if s.get("p0_lineage_status") != "PASS_RET120_FROZEN_SIGNAL_LINEAGE_V0_1":
        raise RuntimeError("FAIL_CLOSED ret120 P0 lineage not PASS")
    fp=s.get("fingerprints",{})
    if fp.get("ret120_frozen_panel_sha256") != EXPECTED_PANEL_SHA or fp.get("ret120_frozen_daily_buckets_sha256") != EXPECTED_BUCKET_SHA:
        raise RuntimeError("FAIL_CLOSED ret120 frozen artifact fingerprint drift")
    m=s.get("main_20bps",{}).get("metrics",{})
    d=s.get("main_20bps",{}).get("diagnostics",{})
    require_close("ret120 CAGR",m.get("cagr"),0.09180689359160388)
    require_close("ret120 MDD",m.get("max_drawdown"),-0.06889145727046919)
    require_close("ret120 Sharpe",m.get("sharpe"),1.1292670006160577)
    if int(d.get("executed_trades",-1)) != EXPECTED_RET_TRADES:
        raise RuntimeError("FAIL_CLOSED ret120 executed trades drift")
    q5=pd.read_csv(RET_Q5,dtype={"symbol":str})
    if len(q5)!=EXPECTED_Q5_ROWS:
        raise RuntimeError(f"FAIL_CLOSED ret120 Q5 rows expected={EXPECTED_Q5_ROWS} actual={len(q5)}")
    return s,f


def trend_family_decision(f: dict) -> dict:
    main=f["main_20bps_full_sample"]; stress=f["stress_50bps_full_sample"]; rnd=f["random_tie_break_20bps"]
    checks={
        "both_standalone_pass": f["standalone_status"]["ret120"]=="PASS_STANDALONE_PORTFOLIO_GATE" and f["standalone_status"]["52wh_h120"]=="PASS_STANDALONE_PORTFOLIO_GATE",
        "ret120_main_cagr_higher": main["delta_ret120_minus_52wh"]["cagr"]>0,
        "ret120_main_sharpe_higher": main["delta_ret120_minus_52wh"]["sharpe"]>0,
        "mdd_near_identical_within_10bp": abs(main["delta_ret120_minus_52wh"]["max_drawdown"])<=0.001,
        "ret120_capital_efficiency_higher": main["delta_ret120_minus_52wh"]["capital_efficiency"]>0,
        "ret120_stress_cagr_higher": stress["delta_ret120_minus_52wh"]["cagr"]>0,
        "ret120_stress_sharpe_higher": stress["delta_ret120_minus_52wh"]["sharpe"]>0,
        "ret120_random_p10_cagr_higher": rnd["delta_ret120_minus_52wh"]["cagr_p10"]>0,
        "ret120_random_median_cagr_higher": rnd["delta_ret120_minus_52wh"]["cagr_median"]>0,
        "ret120_random_p10_sharpe_higher": rnd["delta_ret120_minus_52wh"]["sharpe_p10"]>0,
        "ret120_random_median_sharpe_higher": rnd["delta_ret120_minus_52wh"]["sharpe_median"]>0,
    }
    if not all(checks.values()):
        raise RuntimeError(f"FAIL_CLOSED family-decision evidence changed {checks}")
    return {
        "status":"FROZEN_TREND_FAMILY_REPRESENTATIVE_RET120_V0_1",
        "representative":"RET120_H120",
        "family_alternate":"52WH_H120_FROZEN_BENCHMARK",
        "checks":checks,
        "note":"This freezes the within-family representative only. RET120 is not yet Strategy #2; independence/incremental value versus Deep Oversold is P3/P4.",
    }


def deep_economic_equivalence_approved(summary: dict) -> bool:
    """Return True only for the exact approved V0.1.3 schema.

    V0.1.3 stores the economic-equivalence decision under
    ``frozen_event_replay_on_current_local_history``.  V0.1 accidentally looked
    for a shorter, non-existent key and therefore rejected a valid PASS summary.
    Keep this deliberately strict: do not search arbitrary nested dictionaries
    for a truthy flag.
    """
    status = summary.get("status")
    if status == "PASS_DEEP_OVERSOLD_EXACT_REPLAY_V0_1_3":
        return True
    if status != "PASS_DEEP_OVERSOLD_ECONOMIC_EQUIVALENCE_V0_1_3":
        return False
    replay = summary.get("frozen_event_replay_on_current_local_history", {})
    return replay.get("economic_equivalence_for_alpha2") is True


def verify_deep() -> dict:
    for p in (DEEP_SUMMARY,DEEP_CURVE,DEEP_TRADES,DEEP_FROZEN_EVENTS):
        if not p.exists(): raise RuntimeError(f"FAIL_CLOSED missing Deep artifact {p}")
    s=load_json(DEEP_SUMMARY)
    if s.get("status") not in EXPECTED_DEEP_STATUSES:
        raise RuntimeError(f"FAIL_CLOSED Deep status={s.get('status')}")
    run=s.get("runner",{})
    if run.get("sha256") != EXPECTED_DEEP_RUNNER_SHA:
        raise RuntimeError("FAIL_CLOSED Deep runner fingerprint drift")
    fr=s.get("frozen_lineage",{})
    if fr.get("frozen_dedup_sha256") != EXPECTED_DEEP_DEDUP_SHA or int(fr.get("frozen_dedup_rows",-1)) != EXPECTED_DEEP_FROZEN_ROWS:
        raise RuntimeError("FAIL_CLOSED Deep frozen lineage drift")
    fm=fr.get("frozen_expected_portfolio_metrics",{})
    if int(fm.get("trades",-1)) != EXPECTED_DEEP_FROZEN_TRADES:
        raise RuntimeError("FAIL_CLOSED Deep frozen trade count drift")
    require_close("Deep frozen CAGR",fm.get("cagr"),EXPECTED_DEEP_FROZEN_CAGR)
    require_close("Deep frozen MDD",fm.get("max_drawdown"),EXPECTED_DEEP_FROZEN_MDD)
    require_close("Deep frozen Sharpe",fm.get("sharpe"),EXPECTED_DEEP_FROZEN_SHARPE)
    if not deep_economic_equivalence_approved(s):
        raise RuntimeError("FAIL_CLOSED Deep economic equivalence approval missing/false")
    return s


def check() -> int:
    p1=load_p1_module()
    rs,rf=verify_ret120(); ds=verify_deep(); dec=trend_family_decision(rf)
    out={
        "status":"PASS_RET120_VS_DEEP_INCREMENTAL_V0_1_CHECK",
        "trend_family_decision":dec,
        "ret120_status":rs.get("status"),
        "deep_status":ds.get("status"),
        "methodology":{
            "common_period":"intersection of existing Deep and ret120 daily curves",
            "fixed_combo":"50/50 initial-capital sleeves; no weight optimization; no sleeve rebalance",
            "ret120_random_tie_break_seeds":100,
            "random_combo_replay":True,
            "automatic_strategy2_verdict":False,
        },
        "signal_semantics":{
            "ret120":"exact frozen v0.2.2 Q5 membership emitted by P1; not recomputed here",
            "deep":"frozen 1468-event lineage replay with V0.1.3 economic-equivalence gate",
        },
        "no_network":True,"no_broker":True,"no_database_mutation":True,
    }
    print(json.dumps(out,ensure_ascii=False,indent=2)); return 0


def run() -> int:
    p1=load_p1_module()
    rs,rf=verify_ret120(); ds=verify_deep(); family_decision=trend_family_decision(rf)
    OUT_ROOT.mkdir(parents=True,exist_ok=True)

    deep=normalize_curve(DEEP_CURVE,"deep")
    trend=normalize_curve(RET_CURVE,"trend")
    common,dm,tm,cm=fixed_50_50(deep,trend)

    # Baseline diagnostics.
    corr=float(common.deep_return.corr(common.trend_return))
    ddcorr=float(common.deep_dd.corr(common.trend_dd))
    both_under=(common.deep_dd<0)&(common.trend_dd<0)
    both_dd2=(common.deep_dd<=-0.02)&(common.trend_dd<=-0.02)
    dg=pd.to_numeric(common.deep_gross,errors="coerce").fillna(0.0)
    tg=pd.to_numeric(common.trend_gross,errors="coerce").fillna(0.0)

    deep_tr=normalize_trades(DEEP_TRADES,"Deep")
    trend_tr=normalize_trades(RET_TRADES,"ret120")
    hsum,hdaily=holding_overlap(deep_tr,trend_tr,common.date)

    # Signal overlap is restricted to the ret120 signal window so the key-set
    # comparison is not diluted by old Deep history that ret120 P1 never tested.
    de=normalize_signal_events(DEEP_FROZEN_EVENTS,"Deep")
    re=normalize_signal_events(RET_Q5,"ret120")
    sig_lo=pd.Timestamp(str(rs["definition"]["signal_start"]))
    sig_hi=pd.Timestamp(str(rs["definition"]["signal_cutoff"]))
    de_win=de[(de.signal_date>=sig_lo)&(de.signal_date<=sig_hi)].copy()
    re_win=re[(re.signal_date>=sig_lo)&(re.signal_date<=sig_hi)].copy()
    sig_overlap=key_overlap(de_win,re_win,["symbol","signal_date"])

    trade_cols=["symbol","signal_date"] if "signal_date" in deep_tr.columns and "signal_date" in trend_tr.columns else ["symbol","entry_date"]
    trade_overlap=key_overlap(deep_tr,trend_tr,trade_cols)
    trade_overlap["key_basis"]="+".join(trade_cols)

    # Replay ret120 100 random same-day tie-breaks with the exact P1 simulator.
    q5=pd.read_csv(RET_Q5,dtype={"symbol":str,"signal_date":str,"entry_date":str,"exit_date":str})
    for c in ("signal_date","entry_date","exit_date"):
        q5[c]=q5[c].astype(str).str.replace("-","",regex=False).str.replace(".0","",regex=False).str[:8]
    symbols=sorted(q5.symbol.astype(str).unique())
    latest=str(rs["definition"]["latest_session"])
    sig_start=str(rs["definition"]["signal_start"])
    import duckdb
    con=duckdb.connect(str(p1.CATALOG),read_only=True)
    try:
        panel=p1.load_panel(con,symbols,latest)
        calendar=p1.market_calendar(con,sig_start,latest)
    finally:
        con.close()
    open_map,close_map=p1.price_maps(panel)

    # One exact replay first: detects any mismatch between emitted P1 events and
    # current installed P1 simulator before random sensitivity is trusted.
    baseline=p1.simulate(q5,calendar,open_map,close_map,p1.MAIN_COST,seed=None)
    require_close("P1 exact replay CAGR",baseline["metrics"]["cagr"],rs["main_20bps"]["metrics"]["cagr"])
    require_close("P1 exact replay MDD",baseline["metrics"]["max_drawdown"],rs["main_20bps"]["metrics"]["max_drawdown"])
    require_close("P1 exact replay Sharpe",baseline["metrics"]["sharpe"],rs["main_20bps"]["metrics"]["sharpe"])
    if int(baseline["diagnostics"]["executed_trades"])!=EXPECTED_RET_TRADES:
        raise RuntimeError("FAIL_CLOSED P1 exact replay trade count drift")

    random_rows=[]
    for seed in range(100):
        z=p1.simulate(q5,calendar,open_map,close_map,p1.MAIN_COST,seed=seed)
        rz=z["curve"][["date","equity","gross_weight"]].copy()
        rz["date"]=pd.to_datetime(rz["date"].astype(str),format="%Y%m%d")
        rz["trend_return"]=pd.to_numeric(rz.equity,errors="raise").pct_change(fill_method=None)
        rz.loc[rz.index[0],"trend_return"]=float(rz.equity.iloc[0])/p1.INITIAL_EQUITY-1.0
        rz=rz.rename(columns={"gross_weight":"trend_gross"})[["date","trend_return","trend_gross"]]
        rc,rdm,rtm,rcm=fixed_50_50(deep[["date","deep_return","deep_gross"]],rz)
        random_rows.append({
            "seed":seed,
            "trend_cagr":rtm["cagr"],"trend_mdd":rtm["max_drawdown"],"trend_sharpe":rtm["sharpe"],
            "combo_cagr":rcm["cagr"],"combo_mdd":rcm["max_drawdown"],"combo_sharpe":rcm["sharpe"],
            "delta_combo_vs_deep_cagr":rcm["cagr"]-rdm["cagr"],
            "delta_combo_vs_deep_mdd":rcm["max_drawdown"]-rdm["max_drawdown"],
            "delta_combo_vs_deep_sharpe":rcm["sharpe"]-rdm["sharpe"],
            "daily_return_corr":float(rc.deep_return.corr(rc.trend_return)),
        })
    rdf=pd.DataFrame(random_rows)

    common.to_csv(OUT_ROOT/"common_daily_20bps.csv",index=False,encoding="utf-8-sig")
    hdaily.to_csv(OUT_ROOT/"holding_overlap_daily.csv",index=False,encoding="utf-8-sig")
    rdf.to_csv(OUT_ROOT/"ret120_random_50_50_sensitivity_100.csv",index=False,encoding="utf-8-sig")
    write_json_atomic(OUT_ROOT/"trend_family_decision_v01.json",family_decision)

    out={
        "status":"PASS_RET120_VS_DEEP_INCREMENTAL_VALUE_AUDIT_V0_1",
        "generated_at":pd.Timestamp.now(tz="Asia/Singapore").isoformat(),
        "trend_family_decision":family_decision,
        "common_period":{"start":str(common.date.min().date()),"end":str(common.date.max().date()),"sessions":int(len(common))},
        "methodology":{
            "deep":"Deep Oversold frozen-event replay / V0.1.3 economic equivalence",
            "trend":"RET120_H120 exact frozen v0.2.2 Q5 / P1 comparable economics",
            "fixed_combo":"50/50 initial-capital sleeves; no weight optimization; no daily rebalance",
            "ret120_random_tie_break_seeds":100,
            "strategy2_auto_verdict":False,
        },
        "metrics":{"deep_oversold":dm,"ret120_h120":tm,"fixed_50_50_sleeves":cm},
        "delta_fixed_50_50":{
            "vs_deep":{"cagr":cm["cagr"]-dm["cagr"],"max_drawdown":cm["max_drawdown"]-dm["max_drawdown"],"sharpe":cm["sharpe"]-dm["sharpe"]},
            "vs_ret120":{"cagr":cm["cagr"]-tm["cagr"],"max_drawdown":cm["max_drawdown"]-tm["max_drawdown"],"sharpe":cm["sharpe"]-tm["sharpe"]},
        },
        "correlation_and_drawdown":{
            "daily_return_correlation":corr,"drawdown_correlation":ddcorr,
            "both_underwater_fraction":float(both_under.mean()),
            "both_drawdown_le_2pct_fraction":float(both_dd2.mean()),
        },
        "capital_overlap":{
            "both_active_fraction":float(((dg>0.005)&(tg>0.005)).mean()),
            "mean_min_gross":float(np.minimum(dg,tg).mean()),
            "deep_mean_gross":float(dg.mean()),"ret120_mean_gross":float(tg.mean()),
            "fixed_50_50_mean_gross":float(common.combo_gross.mean()),"fixed_50_50_max_gross":float(common.combo_gross.max()),
            "full_overlay_mean_gross_diagnostic":float(common.full_overlay_gross.mean()),
            "full_overlay_p95_gross_diagnostic":float(common.full_overlay_gross.quantile(.95)),
            "full_overlay_max_gross_diagnostic":float(common.full_overlay_gross.max()),
        },
        "signal_overlap_common_signal_window":sig_overlap,
        "executed_trade_overlap":trade_overlap,
        "holding_overlap":hsum,
        "random_selection_sensitivity":{
            "combo_cagr":qtile(rdf.combo_cagr),"combo_mdd":qtile(rdf.combo_mdd),"combo_sharpe":qtile(rdf.combo_sharpe),
            "delta_combo_vs_deep_cagr":qtile(rdf.delta_combo_vs_deep_cagr),
            "delta_combo_vs_deep_mdd":qtile(rdf.delta_combo_vs_deep_mdd),
            "delta_combo_vs_deep_sharpe":qtile(rdf.delta_combo_vs_deep_sharpe),
            "daily_return_corr":qtile(rdf.daily_return_corr),
            "fraction_seeds_combo_cagr_gt_deep":float((rdf.delta_combo_vs_deep_cagr>0).mean()),
            "fraction_seeds_combo_mdd_better_than_deep":float((rdf.delta_combo_vs_deep_mdd>0).mean()),
            "fraction_seeds_combo_sharpe_gt_deep":float((rdf.delta_combo_vs_deep_sharpe>0).mean()),
        },
        "lineage":{
            "ret120_p1_status":rs.get("status"),"ret120_p0_status":rs.get("p0_lineage_status"),
            "ret120_panel_sha256":rs.get("fingerprints",{}).get("ret120_frozen_panel_sha256"),
            "ret120_buckets_sha256":rs.get("fingerprints",{}).get("ret120_frozen_daily_buckets_sha256"),
            "deep_status":ds.get("status"),"deep_runner_sha256":ds.get("runner",{}).get("sha256"),
            "deep_frozen_dedup_sha256":ds.get("frozen_lineage",{}).get("frozen_dedup_sha256"),
        },
        "decision_guardrail":"P3 does not auto-approve Strategy #2. P4 must inspect fixed-combo deltas, correlations, overlap and 100-seed robustness without changing the 50/50 rule after seeing results.",
        "no_network":True,"no_broker":True,"no_database_mutation":True,
    }
    write_json_atomic(OUT_ROOT/"incremental_value_summary_v01.json",out)
    print(json.dumps(out,ensure_ascii=False,indent=2))
    print(f"OUTPUT: {OUT_ROOT}")
    return 0


def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument("command",choices=["check","run"]); args=ap.parse_args()
    return check() if args.command=="check" else run()

if __name__=="__main__":
    raise SystemExit(main())
