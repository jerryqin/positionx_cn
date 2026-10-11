
from __future__ import annotations
import argparse,hashlib,json
from datetime import datetime
from pathlib import Path
import pandas as pd,yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/full_market_universe_v0_1.yaml"

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""):
            h.update(b)
    return h.hexdigest()

def is_a_share_symbol(s,cfg):
    s=str(s).upper()
    if s.endswith(".SH"):
        return s[:3] in set(cfg["eligibility"]["sh_prefixes"]) and len(s.split(".")[0])==6
    if s.endswith(".SZ"):
        return s[:3] in set(cfg["eligibility"]["sz_prefixes"]) and len(s.split(".")[0])==6
    return False

def required_columns():
    return ["trade_date","symbol","open","high","low","close","pre_close","volume","amount","suspend_flag"]

def schema_check(cfg):
    import duckdb
    con=duckdb.connect(cfg["source"]["market_catalog"],read_only=True)
    try:
        cols=[r[0] for r in con.execute(f"DESCRIBE {cfg['source']['market_table']}").fetchall()]
    finally:
        con.close()
    miss=[c for c in required_columns() if c not in cols]
    return cols,miss

def load_rows(cfg):
    import duckdb
    con=duckdb.connect(cfg["source"]["market_catalog"],read_only=True)
    try:
        table=cfg["source"]["market_table"]
        q=f"""
        SELECT
          CAST(trade_date AS DATE) AS trade_date,
          symbol,
          CAST(open AS DOUBLE) AS open_px,
          CAST(high AS DOUBLE) AS high_px,
          CAST(low AS DOUBLE) AS low_px,
          CAST(close AS DOUBLE) AS close_px,
          CAST(pre_close AS DOUBLE) AS pre_close_px,
          CAST(volume AS DOUBLE) AS volume,
          CAST(amount AS DOUBLE) AS amount,
          CAST(suspend_flag AS INTEGER) AS suspend_flag,
          source
        FROM {table}
        WHERE CAST(trade_date AS DATE) >= CAST(? AS DATE)
          AND (
            RIGHT(symbol,3)='.SH'
            OR RIGHT(symbol,3)='.SZ'
          )
        ORDER BY trade_date,symbol
        """
        z=con.execute(q,[cfg["start_date"]]).fetchdf()
    finally:
        con.close()
    return z.rename(columns={
        "open_px":"open","high_px":"high","low_px":"low",
        "close_px":"close","pre_close_px":"pre_close"
    })

def build_universe(raw,cfg):
    x=raw.copy()
    x["trade_date"]=pd.to_datetime(x["trade_date"])
    x=x[x["symbol"].map(lambda s:is_a_share_symbol(s,cfg))].copy()
    for c in ["open","high","low","close","pre_close","volume","amount"]:
        x[c]=pd.to_numeric(x[c],errors="coerce")

    x["valid_prices"]=(
        x["open"].gt(0)&x["high"].gt(0)&x["low"].gt(0)&
        x["close"].gt(0)&x["pre_close"].gt(0)
    )
    x["volume_positive"]=x["volume"].gt(0)
    x["amount_positive"]=x["amount"].gt(0)
    x["not_suspended"]=x["suspend_flag"].fillna(0).eq(0)
    x["eligible"]=(
        x["valid_prices"]&
        x["volume_positive"]&
        x["amount_positive"]&
        x["not_suspended"]
    )
    u=x[x["eligible"]][["trade_date","symbol"]].drop_duplicates().sort_values(["trade_date","symbol"]).reset_index(drop=True)
    return x,u

def stats(raw_a,u,cfg):
    daily=u.groupby("trade_date")["symbol"].nunique().sort_index()
    yearly={}
    if len(daily):
        tmp=daily.rename("n").reset_index()
        tmp["year"]=tmp["trade_date"].dt.year
        for y,g in tmp.groupby("year"):
            yearly[str(int(y))]={
                "sessions":len(g),
                "median_eligible_symbols":float(g["n"].median()),
                "min_eligible_symbols":int(g["n"].min()),
                "max_eligible_symbols":int(g["n"].max()),
            }
    latest252=daily.tail(252)
    duplicate=int(u.duplicated(["trade_date","symbol"]).sum())
    unique=int(u["symbol"].nunique()) if len(u) else 0
    first=u["trade_date"].min().date().isoformat() if len(u) else None
    last=u["trade_date"].max().date().isoformat() if len(u) else None
    latest_count=int(daily.iloc[-1]) if len(daily) else 0

    q={
        "a_share_source_rows":len(raw_a),
        "valid_price_rate":float(raw_a["valid_prices"].mean()) if len(raw_a) else None,
        "volume_positive_rate":float(raw_a["volume_positive"].mean()) if len(raw_a) else None,
        "amount_positive_rate":float(raw_a["amount_positive"].mean()) if len(raw_a) else None,
        "not_suspended_rate":float(raw_a["not_suspended"].mean()) if len(raw_a) else None,
    }

    g=cfg["readiness_gate"]
    gates={
        "duplicate_symbol_date_rows_max":{
            "threshold":int(g["duplicate_symbol_date_rows_max"]),"actual":duplicate,
            "pass":duplicate<=int(g["duplicate_symbol_date_rows_max"])
        },
        "unique_symbols_min":{
            "threshold":int(g["unique_symbols_min"]),"actual":unique,
            "pass":unique>=int(g["unique_symbols_min"])
        },
        "latest_session_eligible_symbols_min":{
            "threshold":int(g["latest_session_eligible_symbols_min"]),"actual":latest_count,
            "pass":latest_count>=int(g["latest_session_eligible_symbols_min"])
        },
        "first_session_on_or_before":{
            "threshold":g["first_session_on_or_before"],"actual":first,
            "pass":first is not None and first<=g["first_session_on_or_before"]
        },
        "latest_252_session_median_symbols_min":{
            "threshold":int(g["latest_252_session_median_symbols_min"]),
            "actual":float(latest252.median()) if len(latest252) else None,
            "pass":len(latest252)>0 and float(latest252.median())>=int(g["latest_252_session_median_symbols_min"])
        }
    }
    return {
        "eligible_rows":len(u),
        "unique_symbols":unique,
        "first_session":first,
        "latest_session":last,
        "latest_session_eligible_symbols":latest_count,
        "daily_eligible_median":float(daily.median()) if len(daily) else None,
        "daily_eligible_min":int(daily.min()) if len(daily) else None,
        "daily_eligible_max":int(daily.max()) if len(daily) else None,
        "latest_252_session_median_symbols":float(latest252.median()) if len(latest252) else None,
        "yearly":yearly,
        "quality":q,
        "gates":gates,
        "ready":all(v["pass"] for v in gates.values())
    }

def testbed_stats(cfg):
    p=Path(cfg["source"]["csi500_testbed_panel"])
    if not p.exists():
        return {"exists":False}
    sha=sha256_file(p)
    z=pd.read_parquet(p,columns=["trade_date","symbol"])
    return {
        "exists":True,
        "sha256":sha,
        "sha_match":sha==cfg["source"]["expected_csi500_panel_sha256"],
        "rows":len(z),
        "unique_symbols":int(z["symbol"].nunique()),
        "date_min":str(pd.to_datetime(z["trade_date"]).min().date()),
        "date_max":str(pd.to_datetime(z["trade_date"]).max().date()),
        "known_scope":"CSI500_PIT_TESTBED"
    }

def check(cfg):
    cat=Path(cfg["source"]["market_catalog"])
    cols,miss=([],required_columns()) if not cat.exists() else schema_check(cfg)
    out={
        "status":"PASS_FULL_MARKET_UNIVERSE_V0_1_CHECK" if cat.exists() and not miss else "BLOCKED_FULL_MARKET_UNIVERSE_V0_1_CHECK",
        "market_catalog":str(cat),"exists":cat.exists(),
        "market_table":cfg["source"]["market_table"],
        "required_columns":required_columns(),
        "missing_columns":miss,
        "source_columns":cols,
        "research_domain":"SH/SZ A-shares only",
        "purpose":"Replace the CSI500 PIT testbed as the discovery universe before testing more Opportunity Engine mechanisms."
    }
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if out["status"].startswith("PASS_") else 3

def run(cfg):
    cols,miss=schema_check(cfg)
    if miss:
        raise RuntimeError(f"market table missing required columns: {miss}")
    raw=load_rows(cfg)
    a=raw[raw["symbol"].map(lambda s:is_a_share_symbol(s,cfg))].copy()
    annotated,u=build_universe(raw,cfg)
    summary=stats(annotated,u,cfg)
    outdir=Path(cfg["output_dir"]);outdir.mkdir(parents=True,exist_ok=True)

    universe_path=outdir/"full_market_tradable_universe_v0_1.parquet"
    u.to_parquet(universe_path,index=False)

    result={
        "status":"PASS_FULL_MARKET_UNIVERSE_V0_1_RUN" if summary["ready"] else "NOT_READY_FULL_MARKET_UNIVERSE_V0_1",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "asset_id":cfg["asset_id"],
        "research_domain":"SH/SZ A-shares only",
        "source":{
            "market_catalog":cfg["source"]["market_catalog"],
            "market_table":cfg["source"]["market_table"],
            "source_columns":cols,
        },
        "full_market_candidate":summary,
        "comparison_to_old_testbed":testbed_stats(cfg),
        "interpretation":{
            "if_ready":"Use this full-market tradable key panel for NEW Opportunity Engine mechanisms. Do not retroactively rescue failed CSI500 mechanisms.",
            "if_not_ready":"Do not continue mechanism search yet. Resolve local market-data coverage only; do not fetch external event data.",
            "survivorship_note":"Daily-bar presence gives a practical historical tradable universe, but complete historical delisting/ST survivorship is not proven by this audit alone."
        },
        "artifact":{
            "universe_panel":str(universe_path),
            "sha256":sha256_file(universe_path)
        },
        "safety":cfg["safety"]
    }
    (outdir/"full_market_universe_v0_1_summary.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print("OUTPUT:",outdir)
    return 0 if summary["ready"] else 2

def main():
    p=argparse.ArgumentParser();p.add_argument("command",choices=["check","run"]);a=p.parse_args();cfg=load_cfg()
    try:
        raise SystemExit(check(cfg) if a.command=="check" else run(cfg))
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_FULL_MARKET_UNIVERSE_V0_1","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        raise SystemExit(3)

if __name__=="__main__":
    main()
