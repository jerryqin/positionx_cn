
from __future__ import annotations
import argparse, hashlib, json, math, re
from pathlib import Path
import numpy as np
import pandas as pd

FROZEN_PANEL=Path(r"D:\positionx_cn\data\research\edge_discovery\trend_persistence_scan_csi500_panel_v0221.parquet")
FROZEN_PANEL_SHA="66b358a54c33866dd03aae187c1d60379a0317df3c15fe3d453babb430b338f1"

TREND_SRC=Path(r"D:\positionx_cn\research\trend_edge_scan.py")
TREND_SRC_SHA="801fcda9cc7db7287eb8ea766c3e054c7804d0cdd64ba0650196701787f0fe16"

RUNNER_SRC=Path(r"D:\positionx_cn\scripts\run_edge_discovery_trend_replication_v022.py")
RUNNER_SRC_SHA="96dc4bcb8fdbbc3097338aa0f9b2b1c48fb2dcc0a235e0db29cccfd7c3f8e4d1"

QMT_HISTORY=Path(r"D:\positionx_data\research\opportunity_engine\full_market_ret120_qmt_v0_2\qmt_back_ratio_close.parquet")
QMT_HISTORY_SHA="4d0e6dbef7bc8b3ac76f572acaeac66b26af794401c92f7b1ac4afe8e388bb58"

OUT=Path(r"D:\positionx_data\research\opportunity_engine\ret120_lineage_root_cause_v0_3")
START=pd.Timestamp("2020-08-03")
END=pd.Timestamp("2026-04-08")

KEYWORDS=[
    "ret120","monotonic","qcut","rank(","get_market_data_ex",
    "dividend_type","fill_data","shift(120)","pct_change","back_ratio",
    "sanitize","quarantine","feature"
]

def sha256_file(p):
    h=hashlib.sha256()
    with Path(p).open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""):
            h.update(b)
    return h.hexdigest()

def safe(v):
    if v is None or isinstance(v,(str,bool,int)):return v
    if isinstance(v,float):return v if math.isfinite(v) else None
    if isinstance(v,(np.integer,)):return int(v)
    if isinstance(v,(np.floating,)):return safe(float(v))
    if isinstance(v,pd.Timestamp):return v.strftime("%Y-%m-%d")
    return str(v)

def json_write(p,obj):
    Path(p).parent.mkdir(parents=True,exist_ok=True)
    Path(p).write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=safe),encoding="utf-8")

def source_context(path,keywords=KEYWORDS,radius=5):
    lines=Path(path).read_text(encoding="utf-8",errors="replace").splitlines()
    hits=set()
    for i,line in enumerate(lines):
        low=line.lower()
        if any(k.lower() in low for k in keywords):
            for j in range(max(0,i-radius),min(len(lines),i+radius+1)):
                hits.add(j)
    blocks=[]
    cur=[]
    prev=None
    for i in sorted(hits):
        if prev is None or i==prev+1:
            cur.append(i)
        else:
            blocks.append(cur);cur=[i]
        prev=i
    if cur:blocks.append(cur)
    out=[]
    for b in blocks:
        out.append({
            "start_line":b[0]+1,
            "end_line":b[-1]+1,
            "text":"\n".join(f"{i+1:05d}: {lines[i]}" for i in b)
        })
    return out

def discover_cols(df):
    dc=next((c for c in ["trade_date","signal_date","date"] if c in df.columns),None)
    sc=next((c for c in ["symbol","code","ticker"] if c in df.columns),None)
    if not dc or not sc or "ret120" not in df.columns:
        raise RuntimeError(f"unsupported frozen schema date={dc} symbol={sc}")
    return dc,sc

def assign_q5(df,valcol):
    z=df[["signal_date","symbol",valcol]].copy()
    z=z.sort_values(["signal_date","symbol"],kind="stable").reset_index(drop=True)
    z["bucket"]=None
    for _,idx in z.groupby("signal_date",sort=False).groups.items():
        ii=np.asarray(list(idx))
        vals=pd.to_numeric(z.loc[ii,valcol],errors="coerce")
        good=vals.notna() & np.isfinite(vals.to_numpy(dtype=float))
        vi=ii[good.to_numpy()]
        if len(vi)<5:continue
        ranks=pd.Series(z.loc[vi,valcol].to_numpy(),index=vi).rank(method="first")
        try:
            b=pd.qcut(ranks,5,labels=["Q1","Q2","Q3","Q4","Q5"])
        except ValueError:
            continue
        z.loc[vi,"bucket"]=b.astype(str).to_numpy()
    return z[["signal_date","symbol","bucket"]]

def qmt_ret120():
    q=pd.read_parquet(QMT_HISTORY,columns=["trade_date","symbol","close"])
    q["trade_date"]=pd.to_datetime(q["trade_date"])
    q["symbol"]=q["symbol"].astype(str)
    q["close"]=pd.to_numeric(q["close"],errors="coerce")
    q=q.sort_values(["symbol","trade_date"]).reset_index(drop=True)
    q["qmt_ret120"]=q.groupby("symbol",sort=False)["close"].transform(lambda s:s/s.shift(120)-1.0)
    return q[["trade_date","symbol","qmt_ret120"]]

def summarize_diff(x):
    a=pd.to_numeric(x["frozen_ret120"],errors="coerce")
    b=pd.to_numeric(x["qmt_ret120"],errors="coerce")
    fa=a.isna();qb=b.isna()
    both=~fa & ~qb
    d=(a[both]-b[both]).abs()
    qs={}
    if len(d):
        for p in [0,.5,.9,.95,.99,.999,1]:
            qs[str(p)]=float(d.quantile(p))
    return {
      "rows":len(x),
      "frozen_nan_rows":int(fa.sum()),
      "current_qmt_nan_rows":int(qb.sum()),
      "both_nan_rows":int((fa&qb).sum()),
      "frozen_nan_qmt_valid_rows":int((fa&~qb).sum()),
      "frozen_valid_qmt_nan_rows":int((~fa&qb).sum()),
      "both_valid_rows":int(both.sum()),
      "abs_diff_quantiles":qs,
      "abs_diff_le_1e12":int((d<=1e-12).sum()) if len(d) else 0,
      "abs_diff_le_1e10":int((d<=1e-10).sum()) if len(d) else 0,
      "abs_diff_le_1e8":int((d<=1e-8).sum()) if len(d) else 0,
      "abs_diff_le_1e6":int((d<=1e-6).sum()) if len(d) else 0,
      "abs_diff_gt_1pctpt":int((d>0.01).sum()) if len(d) else 0,
      "abs_diff_gt_10pctpt":int((d>0.10).sum()) if len(d) else 0,
    }

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=["check","run"])
    a=ap.parse_args()

    files=[
      ("frozen_panel",FROZEN_PANEL,FROZEN_PANEL_SHA),
      ("trend_source",TREND_SRC,TREND_SRC_SHA),
      ("runner_source",RUNNER_SRC,RUNNER_SRC_SHA),
      ("qmt_history",QMT_HISTORY,QMT_HISTORY_SHA),
    ]
    checks=[]
    for name,p,exp in files:
        act=sha256_file(p) if p.exists() else None
        checks.append({"name":name,"path":str(p),"exists":p.exists(),
                       "sha256":act,"expected_sha256":exp,"sha_match":act==exp})
    ok=all(c["exists"] and c["sha_match"] for c in checks)
    if a.command=="check":
        print(json.dumps({"status":"PASS_RET120_LINEAGE_ROOT_CAUSE_V0_3_CHECK" if ok else "BLOCKED_RET120_LINEAGE_ROOT_CAUSE_V0_3_CHECK",
                          "inputs":checks},ensure_ascii=False,indent=2))
        raise SystemExit(0 if ok else 3)
    if not ok:raise SystemExit(3)

    frozen=pd.read_parquet(FROZEN_PANEL)
    dc,sc=discover_cols(frozen)
    f=frozen[[dc,sc,"ret120"]].rename(columns={dc:"signal_date",sc:"symbol","ret120":"frozen_ret120"}).copy()
    f["signal_date"]=pd.to_datetime(f["signal_date"])
    f["symbol"]=f["symbol"].astype(str)
    f=f[(f["signal_date"]>=START)&(f["signal_date"]<=END)].copy()

    q=qmt_ret120().rename(columns={"trade_date":"signal_date"})
    j=f.merge(q,on=["signal_date","symbol"],how="left",validate="one_to_one")

    fq=assign_q5(j,"frozen_ret120").rename(columns={"bucket":"frozen_bucket"})
    qq=assign_q5(j,"qmt_ret120").rename(columns={"bucket":"qmt_bucket"})
    j=j.merge(fq,on=["signal_date","symbol"],how="left").merge(qq,on=["signal_date","symbol"],how="left")

    f5=j["frozen_bucket"].eq("Q5")
    q5=j["qmt_bucket"].eq("Q5")
    mismatch=f5^q5
    bothvalid=j["frozen_ret120"].notna() & j["qmt_ret120"].notna()

    decomp={
      "q5_symdiff_rows":int(mismatch.sum()),
      "frozen_q5_current_not_q5":int((f5&~q5).sum()),
      "current_q5_frozen_not_q5":int((q5&~f5).sum()),
      "q5_mismatch_with_both_ret120_valid":int((mismatch&bothvalid).sum()),
      "q5_mismatch_with_at_least_one_ret120_missing":int((mismatch&~bothvalid).sum()),
      "frozen_q5_current_ret120_missing":int((f5&j["qmt_ret120"].isna()).sum()),
      "current_q5_frozen_ret120_missing":int((q5&j["frozen_ret120"].isna()).sum()),
    }

    by_year=[]
    j["year"]=j["signal_date"].dt.year
    for y,g in j.groupby("year"):
        ff=g["frozen_bucket"].eq("Q5");qqq=g["qmt_bucket"].eq("Q5")
        bb=g["frozen_ret120"].notna()&g["qmt_ret120"].notna()
        by_year.append({
          "year":int(y),"rows":len(g),
          "frozen_nan":int(g["frozen_ret120"].isna().sum()),
          "qmt_nan":int(g["qmt_ret120"].isna().sum()),
          "q5_mismatch":int((ff^qqq).sum()),
          "q5_mismatch_both_valid":int(((ff^qqq)&bb).sum())
        })

    top_symbols=(j.assign(mismatch=mismatch.astype(int))
                   .groupby("symbol")["mismatch"].sum()
                   .sort_values(ascending=False).head(30))
    top_dates=(j.assign(mismatch=mismatch.astype(int))
                 .groupby("signal_date")["mismatch"].sum()
                 .sort_values(ascending=False).head(30))

    src={
      "trend_edge_scan.py":source_context(TREND_SRC),
      "run_edge_discovery_trend_replication_v022.py":source_context(RUNNER_SRC),
    }

    OUT.mkdir(parents=True,exist_ok=True)
    j.loc[mismatch | j["frozen_ret120"].isna() | j["qmt_ret120"].isna(),
          ["signal_date","symbol","frozen_ret120","qmt_ret120","frozen_bucket","qmt_bucket"]].head(20000).to_csv(
              OUT/"ret120_lineage_difference_sample.csv",index=False)

    result={
      "status":"PASS_RET120_LINEAGE_ROOT_CAUSE_V0_3_RUN",
      "inputs":checks,
      "window":{"start":str(START.date()),"end":str(END.date())},
      "missingness_and_value_diff":summarize_diff(j),
      "q5_mismatch_decomposition":decomp,
      "by_year":by_year,
      "top_mismatch_symbols":[{"symbol":k,"rows":int(v)} for k,v in top_symbols.items()],
      "top_mismatch_dates":[{"signal_date":str(pd.Timestamp(k).date()),"rows":int(v)} for k,v in top_dates.items()],
      "canonical_source_context":src,
      "interpretation_guardrails":[
        "Do not call current QMT history equivalent to the frozen v0.2.2 vintage unless the source semantics match and value/key differences are explained.",
        "Do not relax the frozen Q5 lineage gate.",
        "Do not run full-market RET120 gate until monotonicity is taken from the exact historical canonical source rather than reimplemented by assumption."
      ]
    }
    json_write(OUT/"ret120_lineage_root_cause_v0_3.json",result)

    txt=[]
    txt.append("RET120 LINEAGE ROOT CAUSE V0.3\n")
    txt.append(json.dumps({k:v for k,v in result.items() if k!="canonical_source_context"},
                          ensure_ascii=False,indent=2,default=safe))
    txt.append("\n\n=== CANONICAL SOURCE CONTEXT ===\n")
    for name,blocks in src.items():
        txt.append(f"\n### {name}\n")
        for b in blocks:
            txt.append(f"\n--- lines {b['start_line']}-{b['end_line']} ---\n{b['text']}\n")
    (OUT/"ret120_lineage_root_cause_v0_3.txt").write_text("\n".join(txt),encoding="utf-8")

    print(json.dumps({k:v for k,v in result.items() if k!="canonical_source_context"},
                     ensure_ascii=False,indent=2,default=safe))
    print("SOURCE_CONTEXT:",OUT/"ret120_lineage_root_cause_v0_3.txt")
    print("OUTPUT:",OUT)
    raise SystemExit(0)

if __name__=="__main__":
    main()
