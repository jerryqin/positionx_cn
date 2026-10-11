
from __future__ import annotations
import argparse, hashlib, json, re
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/e1_identity_qmt_verification_v0_2.yaml"

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def sha256_file(p):
    h=hashlib.sha256()
    with Path(p).open("rb") as f:
        for c in iter(lambda:f.read(1024*1024),b""):
            h.update(c)
    return h.hexdigest()

def xt():
    from xtquant import xtdata
    return xtdata

def code6(s):
    m=re.search(r"(\d{6})",str(s or "").upper())
    return m.group(1) if m else None

def canonical_qmt_symbol(row):
    code=code6(row["symbol"])
    if not code:
        return None
    p2=code[:2]
    if p2 in {"20","90"}:
        return None
    if p2 in {"43","83","87","88","92"}:
        return f"{code}.BJ"
    if p2 in {"00","30"}:
        return f"{code}.SZ"
    if p2 in {"60","68"}:
        return f"{code}.SH"
    return None

def parse_time_col(v):
    if isinstance(v,(int,np.integer,float,np.floating)) and not pd.isna(v):
        iv=int(v)
        if iv>10**11:
            return pd.to_datetime(iv,unit="ms",errors="coerce").normalize()
        s=str(iv)
    else:
        s=str(v)
    digits=re.sub(r"\D","",s)
    if len(digits)>=13:
        try:
            return pd.to_datetime(int(digits[:13]),unit="ms",errors="coerce").normalize()
        except Exception:
            pass
    if len(digits)>=8:
        try:
            return pd.to_datetime(digits[:8],format="%Y%m%d",errors="coerce").normalize()
        except Exception:
            pass
    return pd.to_datetime(v,errors="coerce").normalize()

def flatten_qmt(data, symbols, fields):
    rows=[]
    if not isinstance(data,dict):
        return pd.DataFrame()
    for fld in fields:
        frame=data.get(fld)
        if frame is None or not isinstance(frame,pd.DataFrame):
            continue
        for sym in symbols:
            if sym not in frame.index:
                continue
            ser=frame.loc[sym]
            for t,val in ser.items():
                dt=parse_time_col(t)
                if pd.isna(dt):
                    continue
                rows.append({
                    "qmt_symbol":sym,
                    "trade_date":dt,
                    "field":fld,
                    "qmt_value":val,
                })
    if not rows:
        return pd.DataFrame()
    z=pd.DataFrame(rows)
    z["qmt_value"]=pd.to_numeric(z["qmt_value"],errors="coerce")
    return (
        z.pivot_table(
            index=["qmt_symbol","trade_date"],
            columns="field",
            values="qmt_value",
            aggfunc="first"
        )
        .reset_index()
    )

def fetch_qmt(sample,cfg,do_download=False,download_contracts=False):
    xtdata=xt()
    symbols=sorted(sample["qmt_symbol"].dropna().unique().tolist())
    if download_contracts:
        try:
            xtdata.download_history_contracts()
        except Exception as e:
            print(f"WARN download_history_contracts: {e}")

    start=pd.to_datetime(sample["trade_date"]).min().strftime("%Y%m%d")
    end=pd.to_datetime(sample["trade_date"]).max().strftime("%Y%m%d")

    if do_download:
        def cb(d):
            if isinstance(d,dict):
                print(
                    f"DOWNLOAD {d.get('finished')}/{d.get('total')} "
                    f"{d.get('stockcode')} {d.get('message','')}"
                )
        xtdata.download_history_data2(
            symbols,
            "1d",
            start_time=start,
            end_time=end,
            callback=cb,
            incrementally=False,
        )

    data=xtdata.get_market_data(
        field_list=cfg["qmt_verification"]["fields"],
        stock_list=symbols,
        period="1d",
        start_time=start,
        end_time=end,
        count=-1,
        dividend_type=cfg["qmt_verification"]["dividend_type"],
        fill_data=bool(cfg["qmt_verification"]["fill_data"]),
    )
    return flatten_qmt(
        data,
        symbols,
        cfg["qmt_verification"]["fields"],
    )

def compare(sample,qmt,cfg):
    field_map={
        "eva_open":"open",
        "eva_high":"high",
        "eva_low":"low",
        "eva_close":"close",
        "eva_pre_close":"preClose",
    }

    z=sample.merge(
        qmt,
        on=["qmt_symbol","trade_date"],
        how="left",
    )

    abs_tol=float(cfg["qmt_verification"]["abs_tolerance"])
    rel_tol=float(cfg["qmt_verification"]["relative_tolerance"])
    catastrophic_rel=float(
        cfg["qmt_verification"]["catastrophic_relative_error"]
    )

    field_stats={}
    field_comparable_cols=[]
    field_pass_cols=[]
    catastrophic=pd.Series(False,index=z.index)

    for eva_col,qmt_col in field_map.items():
        eva=pd.to_numeric(z[eva_col],errors="coerce")
        qv=pd.to_numeric(
            z[qmt_col] if qmt_col in z.columns else pd.Series(np.nan,index=z.index),
            errors="coerce",
        )

        # Frozen V0.2 intended semantics:
        # QMT zero is a no-price/non-comparable placeholder, not a mismatch.
        comparable=eva.notna() & qv.notna() & qv.ne(0)

        diff=(eva-qv).abs()
        tol=np.maximum(abs_tol,qv.abs()*rel_tol)
        passed=comparable & diff.le(tol)
        rel=diff/qv.abs()

        catastrophic |= (
            comparable & rel.gt(catastrophic_rel)
        )

        ccol=f"comparable_{qmt_col}"
        pcol=f"pass_{qmt_col}"
        z[ccol]=comparable
        z[pcol]=passed
        field_comparable_cols.append(ccol)
        field_pass_cols.append(pcol)

        field_stats[qmt_col]={
            "comparable_points":int(comparable.sum()),
            "pass_points":int(passed.sum()),
            "pass_rate":(
                float(passed.sum()/comparable.sum())
                if comparable.sum() else None
            ),
            "max_abs_diff":(
                float(diff[comparable].max())
                if comparable.any() else None
            ),
            "max_rel_diff":(
                float(rel[comparable].max())
                if comparable.any() else None
            ),
        }

    # Hotfix:
    # A row is all-fields comparable iff EVERY FIELD is independently comparable.
    # This makes row-level and field-level denominators logically consistent.
    z["all_fields_comparable"]=z[field_comparable_cols].all(axis=1)
    z["all_fields_pass"]=(
        z["all_fields_comparable"]
        & z[field_pass_cols].all(axis=1)
    )
    z["catastrophic"]=catastrophic

    # Diagnostic only: rows excluded solely because QMT has zero in >=1 field.
    qmt_cols=list(field_map.values())
    qmt_nonnull=pd.Series(True,index=z.index)
    any_qmt_zero=pd.Series(False,index=z.index)
    for qcol in qmt_cols:
        qv=pd.to_numeric(
            z[qcol] if qcol in z.columns else pd.Series(np.nan,index=z.index),
            errors="coerce"
        )
        qmt_nonnull &= qv.notna()
        any_qmt_zero |= qv.eq(0)

    eva_nonnull=pd.Series(True,index=z.index)
    for ecol in field_map:
        eva_nonnull &= pd.to_numeric(z[ecol],errors="coerce").notna()

    z["excluded_qmt_zero_row"]=(
        eva_nonnull
        & qmt_nonnull
        & any_qmt_zero
        & ~z["all_fields_comparable"]
    )

    comparable_points=int(z["all_fields_comparable"].sum())
    comparable_symbols=int(
        z.loc[z["all_fields_comparable"],"qmt_symbol"].nunique()
    )
    all_pass_rate=(
        float(
            z.loc[
                z["all_fields_comparable"],
                "all_fields_pass",
            ].mean()
        )
        if comparable_points else None
    )

    bse=z[
        z["qmt_symbol"]
        .astype("string")
        .str.endswith(".BJ",na=False)
    ]
    bse_comp=bse["all_fields_comparable"]
    bse_symbols=int(
        bse.loc[bse_comp,"qmt_symbol"].nunique()
    )
    bse_rate=(
        float(bse.loc[bse_comp,"all_fields_pass"].mean())
        if bse_comp.any() else None
    )

    g=cfg["qmt_verification"]
    checks={
        "comparable_symbols_min":
            comparable_symbols>=int(g["comparable_symbols_min"]),
        "comparable_points_min":
            comparable_points>=int(g["comparable_points_min"]),
        "all_fields_row_pass_rate_min":
            all_pass_rate is not None
            and all_pass_rate>=float(g["all_fields_row_pass_rate_min"]),
        "each_field_pass_rate_min":
            all(
                v["pass_rate"] is not None
                and v["pass_rate"]>=float(g["each_field_pass_rate_min"])
                for v in field_stats.values()
            ),
        "catastrophic_rows_max":
            int(z["catastrophic"].sum())
            <=int(g["catastrophic_rows_max"]),
        "bse_comparable_symbols_min":
            bse_symbols>=int(g["bse_comparable_symbols_min"]),
        "bse_all_fields_row_pass_rate_min":
            bse_rate is not None
            and bse_rate>=float(g["bse_all_fields_row_pass_rate_min"]),
    }

    accepted=all(checks.values())

    return z,{
        "verifier_version":"V0.2.1_COMPARABILITY_HOTFIX",
        "sample_equity_symbols":int(sample["qmt_symbol"].nunique()),
        "sample_equity_points":int(len(sample)),
        "comparable_symbols":comparable_symbols,
        "comparable_points":comparable_points,
        "excluded_qmt_zero_rows":int(z["excluded_qmt_zero_row"].sum()),
        "all_fields_row_pass_rate":all_pass_rate,
        "catastrophic_rows":int(z["catastrophic"].sum()),
        "bse_comparable_symbols":bse_symbols,
        "bse_all_fields_row_pass_rate":bse_rate,
        "field_stats":field_stats,
        "checks":checks,
        "accepted":accepted,
        "verdict":(
            "ACCEPT_QMT_INDEPENDENT_PRICE_VERIFICATION_FOR_E1_RESEARCH"
            if accepted else
            "HOLD_QMT_INDEPENDENT_PRICE_VERIFICATION_FOR_E1_RESEARCH"
        ),
    }

def run(cfg,do_download=False,download_contracts=False):
    p=Path(cfg["independent_price_request"])
    if sha256_file(p)!=cfg["expected"]["independent_price_request_sha256"]:
        raise RuntimeError("independent price request SHA mismatch")

    sample=pd.read_csv(p)
    sample["trade_date"]=pd.to_datetime(
        sample["trade_date"],
        errors="coerce",
    ).dt.normalize()
    sample["qmt_symbol"]=sample.apply(
        canonical_qmt_symbol,
        axis=1,
    )
    sample=sample[sample["qmt_symbol"].notna()].copy()

    qmt=fetch_qmt(
        sample,
        cfg,
        do_download=do_download,
        download_contracts=download_contracts,
    )
    comp,summary=compare(sample,qmt,cfg)

    out=Path(cfg["output_dir"])
    out.mkdir(parents=True,exist_ok=True)

    cp=out/"e1_qmt_price_comparison_v0_2_1.csv"
    comp.to_csv(cp,index=False,encoding="utf-8-sig")

    qp=out/"e1_qmt_fetched_points_v0_2_1.parquet"
    qmt.to_parquet(qp,index=False)

    result={
        "status":"PASS_E1_QMT_INDEPENDENT_PRICE_VERIFICATION_V0_2_1_RUN",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "research_id":cfg["research_id"],
        "mode":{
            "download":do_download,
            "download_history_contracts":download_contracts,
        },
        "summary":summary,
        "artifacts":{
            "comparison_csv":str(cp),
            "qmt_points_parquet":str(qp),
        },
        "safety":{
            "e1_outcomes_read":False,
            "canonical_promotion":False,
            "gate_thresholds_changed":False,
            "sample_changed":False,
        },
    }

    # Keep the same canonical summary filename so existing finalize script
    # consumes the corrected verifier result without further logic changes.
    sp=out/"e1_qmt_independent_price_verification_v0_2_summary.json"
    sp.write_text(
        json.dumps(result,ensure_ascii=False,indent=2),
        encoding="utf-8",
    )

    print(json.dumps(result,ensure_ascii=False,indent=2))
    print(f"OUTPUT: {out}")
    return 0

def check(cfg):
    p=Path(cfg["independent_price_request"])
    out={
        "status":(
            "PASS_E1_QMT_INDEPENDENT_PRICE_VERIFICATION_V0_2_1_CHECK"
            if p.exists()
            else
            "BLOCKED_E1_QMT_INDEPENDENT_PRICE_VERIFICATION_V0_2_1_CHECK"
        ),
        "request":str(p),
        "exists":p.exists(),
        "request_sha256":sha256_file(p) if p.exists() else None,
        "expected_sha256":cfg["expected"]["independent_price_request_sha256"],
        "hotfix":"all_fields_comparable = AND(field-level comparable masks)",
        "gate_thresholds_changed":False,
        "sample_changed":False,
    }
    if p.exists() and out["request_sha256"]!=out["expected_sha256"]:
        out["status"]="BLOCKED_E1_QMT_REQUEST_SHA_MISMATCH"
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if out["status"].startswith("PASS") else 3

def main():
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=["check","local","download"])
    p.add_argument("--download-history-contracts",action="store_true")
    a=p.parse_args()
    cfg=load_cfg()

    try:
        if a.command=="check":
            rc=check(cfg)
        elif a.command=="local":
            rc=run(cfg,False,a.download_history_contracts)
        else:
            rc=run(cfg,True,a.download_history_contracts)
        raise SystemExit(rc)
    except Exception as e:
        print(json.dumps({
            "status":"FAIL_CLOSED_E1_QMT_INDEPENDENT_PRICE_VERIFICATION_V0_2_1",
            "error":f"{type(e).__name__}: {e}",
        },ensure_ascii=False,indent=2))
        raise SystemExit(3)

if __name__=="__main__":
    main()
