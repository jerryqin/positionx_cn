from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = PROJECT_ROOT / "scripts" / "run_deep_oversold_csi500_universe.py"
DEEP_TRADES = Path(
    r"D:\positionx_data\research\alpha2_incremental_value_v0_1"
    r"\deep_oversold_export\deep_oversold_20bps_trades.csv"
)
OUT_ROOT = Path(
    r"D:\positionx_data\research\earnings_residual_capacity_v0_1"
    r"\deep_trade_semantics_diag_v0_1"
)

EXPECTED_RUNNER_SHA256 = (
    "e18792cb9f3676bc515d937fe555cab7d28e4f2e94f918385bace4f54e51e73c"
)

TARGET_SYMBOL = "688208.SH"
TARGET_ENTRY = "20240201"
TARGET_EXIT = "20240307"


def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(8*1024*1024), b""):
            h.update(b)
    return h.hexdigest()


def load_runner():
    if not RUNNER.exists():
        raise RuntimeError(f"missing runner: {RUNNER}")
    actual=sha256_file(RUNNER)
    if actual!=EXPECTED_RUNNER_SHA256:
        raise RuntimeError(
            f"runner hash mismatch expected={EXPECTED_RUNNER_SHA256} actual={actual}"
        )
    spec=importlib.util.spec_from_file_location("px_trade_semantics_runner", str(RUNNER))
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load runner")
    mod=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=mod
    spec.loader.exec_module(mod)
    return mod,actual


def d8(v):
    return str(v).replace("-","").replace(".0","")[:8]


def main() -> int:
    mod,runner_sha=load_runner()
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    # Capture exact simulator implementation currently imported by frozen runner.
    fn=mod.simulate_shared_equity
    fn_source=inspect.getsource(fn)
    fn_file=Path(inspect.getsourcefile(fn) or "")
    source_path=OUT_ROOT/"simulate_shared_equity_source.txt"
    source_path.write_text(fn_source, encoding="utf-8")

    compact_source=inspect.getsource(mod.compact_result)
    compact_path=OUT_ROOT/"compact_result_source.txt"
    compact_path.write_text(compact_source, encoding="utf-8")

    trades=pd.read_csv(DEEP_TRADES)
    x=trades.copy()
    x["symbol"]=x["symbol"].astype(str)
    x["entry_date8"]=x["entry_date"].map(d8)
    x["exit_date8"]=x["exit_date"].map(d8)
    hit=x[
        (x["symbol"]==TARGET_SYMBOL)
        & (x["entry_date8"]==TARGET_ENTRY)
        & (x["exit_date8"]==TARGET_EXIT)
    ].copy()
    if len(hit)!=1:
        raise RuntimeError(f"expected exactly one target trade, got {len(hit)}")
    row=hit.iloc[0].to_dict()

    adapter=mod.XtDataWatchlistAdapter(
        mod.xtdata,
        mod.AdapterConfig(sector="", bars=2000, dividend_type="front"),
    )
    loaded=adapter.history([TARGET_SYMBOL])
    if TARGET_SYMBOL not in loaded or loaded[TARGET_SYMBOL] is None:
        raise RuntimeError("target QMT history missing")
    h=mod.normalize_history(loaded[TARGET_SYMBOL])
    h.index=h.index.astype(str)

    if TARGET_ENTRY not in h.index or TARGET_EXIT not in h.index:
        raise RuntimeError(
            f"target dates missing entry={TARGET_ENTRY in h.index} exit={TARGET_EXIT in h.index}"
        )

    entry_open=float(h.loc[TARGET_ENTRY,"open"])
    entry_close=float(h.loc[TARGET_ENTRY,"close"])
    exit_open=float(h.loc[TARGET_EXIT,"open"])
    exit_close=float(h.loc[TARGET_EXIT,"close"])

    entry_notional=float(row["entry_notional"])
    exit_notional=float(row["exit_notional"])
    shares_naive=entry_notional/entry_open
    exit_recon=shares_naive*exit_close

    relevant_lines=[]
    for i,line in enumerate(fn_source.splitlines(),1):
        ll=line.lower()
        if any(k in ll for k in (
            "entry_notional","exit_notional","shares","entry_price",
            "exit_price","round_trip_cost","fee","gross_market",
            "trades_df","active","notional"
        )):
            relevant_lines.append(f"{i:04d}: {line}")

    diag={
        "status":"PASS_DEEP_TRADE_SEMANTICS_DIAG_V0_1",
        "runner_sha256":runner_sha,
        "simulator_source_file":str(fn_file),
        "simulator_source_sha256":hashlib.sha256(fn_source.encode("utf-8")).hexdigest(),
        "compact_result_source_sha256":hashlib.sha256(compact_source.encode("utf-8")).hexdigest(),
        "deep_trades":{
            "path":str(DEEP_TRADES),
            "sha256":sha256_file(DEEP_TRADES),
            "columns":list(trades.columns),
            "rows":int(len(trades)),
        },
        "target_trade":row,
        "current_qmt":{
            "symbol":TARGET_SYMBOL,
            "entry_date":TARGET_ENTRY,
            "entry_open":entry_open,
            "entry_close":entry_close,
            "exit_date":TARGET_EXIT,
            "exit_open":exit_open,
            "exit_close":exit_close,
        },
        "naive_reconstruction":{
            "shares_entry_notional_div_entry_open":shares_naive,
            "shares_times_exit_close":exit_recon,
            "exported_exit_notional":exit_notional,
            "absolute_diff":exit_recon-exit_notional,
            "relative_diff":abs(exit_recon-exit_notional)/max(abs(exit_recon),abs(exit_notional),1.0),
        },
        "simulator_relevant_source_lines":relevant_lines,
        "outputs":{
            "simulator_source":str(source_path),
            "compact_result_source":str(compact_path),
        },
        "safety":{
            "qmt_download_called":False,
            "qmt_refresh_called":False,
            "broker_used":False,
            "database_mutation":False,
        },
    }
    out=OUT_ROOT/"deep_trade_semantics_diag.json"
    out.write_text(json.dumps(diag,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    print(json.dumps(diag,ensure_ascii=False,indent=2,default=str))
    print(f"OUTPUT: {out}")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
