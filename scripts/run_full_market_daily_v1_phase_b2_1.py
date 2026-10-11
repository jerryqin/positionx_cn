from __future__ import annotations

"""
PositionX — Full Market Daily Data V1 / Phase B2.1
Refined representative full-history pilot.

Changes from B2:
1) Only supported A-share boards are eligible:
   SSE_MAIN / SZSE_MAIN / CHINEXT / STAR.
   This also prevents B-shares such as 900xxx.SH from entering archive probes.
2) Current universe excludes:
   - BSE
   - current ST/*ST/S*ST/SST/NST
   - non-verified-stock entries
   - entries with no local daily history at all in Phase A
     (primarily pre-listing / IPO-pipeline codes that can appear in QMT umbrella sectors)
3) Historical ST remains a PIT eligibility problem; raw price history is not destroyed.

This pilot downloads only a small sample, never the whole market.
"""

import argparse, csv, json, math, re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PHASE_A_ROOT = PROJECT_ROOT/"data"/"research"/"full_market_daily_v1_audit"
PHASE_B1_ROOT = PROJECT_ROOT/"data"/"research"/"full_market_daily_v1_phase_b1"
OUTPUT_ROOT = PROJECT_ROOT/"data"/"research"/"full_market_daily_v1_phase_b2_1"

ALLOWED_BOARDS = {"SSE_MAIN","SZSE_MAIN","CHINEXT","STAR"}
ST_RE = re.compile(r"^(?:\*?ST|S\*?ST|SST|NST)", re.I)


def safe(v):
    if v is None or isinstance(v,(str,int,float,bool)):
        if isinstance(v,float) and not math.isfinite(v): return None
        return v
    if hasattr(v,"item"):
        try: return safe(v.item())
        except Exception: pass
    return str(v)

def write_json(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=safe),encoding="utf-8")

def write_csv(path,rows,fields=None):
    path.parent.mkdir(parents=True,exist_ok=True)
    if fields is None:
        seen=set(); fields=[]
        for r in rows:
            for k in r:
                if k not in seen:
                    seen.add(k); fields.append(k)
        if not fields: fields=["empty"]
    with path.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(fields),extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k:safe(r.get(k)) for k in fields})

def read_csv(path):
    with path.open("r",encoding="utf-8-sig",newline="") as f:
        return list(csv.DictReader(f))

def latest_dir(root,required):
    for d in sorted((p for p in root.iterdir() if p.is_dir()),key=lambda p:p.name,reverse=True):
        if all((d/x).exists() for x in required): return d
    raise RuntimeError(f"No complete artifact under {root}")

def boolish(v):
    return str(v).strip().lower() in {"1","true","yes","y"}

def clean_name(v):
    return str(v or "").replace(" ","").replace("　","").replace("＊","*").strip()

def is_st(v):
    return bool(ST_RE.match(clean_name(v).upper()))

def parse_int(v,default=0):
    try: return int(float(v))
    except Exception: return default

def norm_date(v):
    if v is None: return None
    s=str(v).strip()
    if not s or s.lower() in {"nan","nat","none"}: return None
    d=re.sub(r"\D","",s)
    if len(d)>=8 and d[:4].isdigit() and 1990<=int(d[:4])<=2200: return d[:8]
    try:
        x=float(s)
        if x>1e12:x/=1000
        if x>1e9:return datetime.fromtimestamp(x).strftime("%Y%m%d")
    except Exception: pass
    return None

def current_reason(master_row,cache_row):
    board=master_row.get("board")
    sym=str(master_row.get("symbol","")).upper()
    if board=="BSE" or sym.endswith(".BJ"):
        return False,"EXCLUDE_BSE"
    if board not in ALLOWED_BOARDS:
        return False,"EXCLUDE_UNSUPPORTED_BOARD"
    if not boolish(master_row.get("instrument_type_stock")):
        return False,"EXCLUDE_NOT_VERIFIED_STOCK"
    if is_st(master_row.get("name")):
        return False,"EXCLUDE_CURRENT_ST"
    if parse_int((cache_row or {}).get("recent_close_bars"))<=0:
        return False,"EXCLUDE_NO_DAILY_HISTORY_CURRENT"
    return True,"ELIGIBLE_CURRENT_NON_ST_LISTED_SH_SZ"

def local_data_call(xtdata,fields,symbols,count=-1):
    try:
        return xtdata.get_local_data(field_list=fields,stock_list=symbols,period="1d",
            start_time="",end_time="",count=count,dividend_type="none",fill_data=False)
    except TypeError:
        return xtdata.get_local_data(fields,symbols,"1d","","",count,"none",False)

def series_for(raw,field,symbol):
    if not isinstance(raw,dict): return None
    obj=raw.get(field)
    if obj is not None:
        try:
            if hasattr(obj,"index") and symbol in obj.index:return obj.loc[symbol]
            if hasattr(obj,"columns") and symbol in obj.columns:return obj[symbol]
        except Exception: pass
    obj=raw.get(symbol)
    if obj is not None:
        try:
            if hasattr(obj,"columns") and field in obj.columns:return obj[field]
            if isinstance(obj,dict) and field in obj:return obj[field]
        except Exception: pass
    return None

def item_map(series):
    if series is None:return {}
    try:
        s=series.dropna()
        return {d:safe(v) for k,v in s.items() if (d:=norm_date(k))}
    except Exception: pass
    if isinstance(series,dict):
        return {d:safe(v) for k,v in series.items() if v is not None and (d:=norm_date(k))}
    return {}

def download_one(xtdata,symbol,start):
    try:
        try: xtdata.download_history_data(symbol,"1d",start,"",False)
        except TypeError: xtdata.download_history_data(symbol,"1d",start,"")
        return True,None
    except Exception as e:
        return False,f"{type(e).__name__}: {e}"

def collect(xtdata,symbol,fields):
    raw=local_data_call(xtdata,fields,[symbol],-1)
    maps={f:item_map(series_for(raw,f,symbol)) for f in fields}
    dates=sorted(set().union(*(set(m) for m in maps.values())))
    out=[]
    for d in dates:
        r={"symbol":symbol,"trade_date":d}
        for f in fields:r[f]=maps[f].get(d)
        out.append(r)
    return out

def validate(meta,rows):
    dates=[r["trade_date"] for r in rows]
    bad_ohlc=0; nonpos=0
    for r in rows:
        try:
            o,h,l,c=map(float,[r.get("open"),r.get("high"),r.get("low"),r.get("close")])
            if c<=0:nonpos+=1
            if h<max(o,c,l) or l>min(o,c,h):bad_ohlc+=1
        except Exception:pass
    n=len(rows)
    return {
        "symbol":meta["symbol"],"sample_role":meta["sample_role"],"board":meta.get("board"),
        "name":meta.get("name"),"open_date_master":meta.get("open_date"),
        "expire_date_master":meta.get("expire_date"),"bars":n,
        "first_date":min(dates) if dates else None,"last_date":max(dates) if dates else None,
        "duplicate_dates":len(dates)-len(set(dates)),
        "close_nonnull":sum(r.get("close") is not None for r in rows),
        "amount_nonnull":sum(r.get("amount") is not None for r in rows),
        "preclose_nonnull":sum(r.get("preClose") is not None for r in rows),
        "suspendflag_nonnull":sum(r.get("suspendFlag") is not None for r in rows),
        "amount_coverage":sum(r.get("amount") is not None for r in rows)/n if n else 0,
        "preclose_coverage":sum(r.get("preClose") is not None for r in rows)/n if n else 0,
        "suspendflag_coverage":sum(r.get("suspendFlag") is not None for r in rows)/n if n else 0,
        "bad_ohlc_rows":bad_ohlc,"zero_or_negative_close_rows":nonpos,
    }

def pick_current(eligible,maxn):
    idx={r["symbol"]:r for r in eligible}
    chosen=[]
    anchors=["000001.SZ","000333.SZ","002594.SZ","300750.SZ","600000.SH","600519.SH","601318.SH","688981.SH"]
    for s in anchors:
        if s in idx and idx[s] not in chosen: chosen.append(idx[s])
    groups=defaultdict(list)
    for r in eligible:groups[r["board"]].append(r)
    for bd in ("SSE_MAIN","SZSE_MAIN","CHINEXT","STAR"):
        vals=sorted(groups[bd],key=lambda r:((r.get("open_date") or "99999999"),r["symbol"]))
        for r in vals[:3]+list(reversed(vals[-3:])):
            if r not in chosen:chosen.append(r)
    return chosen[:maxn]

def pick_expired(rows,maxn):
    vals=[]
    for r in rows:
        sym=str(r.get("symbol","")).upper()
        if r.get("board") not in ALLOWED_BOARDS: continue
        if not sym.endswith((".SH",".SZ")): continue
        if not boolish(r.get("instrument_type_stock")): continue
        if is_st(r.get("name")): continue
        rr=dict(r);rr["sample_role"]="EXPIRED_ARCHIVE_PROBE";rr["eligibility_reason"]="ARCHIVE_ONLY"
        vals.append(rr)
    vals.sort(key=lambda r:((r.get("expire_date") or "99999999"),r["symbol"]))
    if not vals or maxn<=0:return []
    if len(vals)<=maxn:return vals
    idxs=sorted({round(i*(len(vals)-1)/(maxn-1)) for i in range(maxn)}) if maxn>1 else [0]
    return [vals[i] for i in idxs[:maxn]]

def parquet_test(df,path):
    try:
        import duckdb
        con=duckdb.connect(":memory:")
        con.register("d",df)
        p=str(path).replace("'","''")
        con.execute(f"COPY d TO '{p}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        n=con.execute(f"SELECT count(*) FROM read_parquet('{p}')").fetchone()[0]
        con.close()
        return {"ok":True,"rows":int(n),"error":None}
    except Exception as e:
        return {"ok":False,"rows":None,"error":f"{type(e).__name__}: {e}"}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--current-sample",type=int,default=24)
    ap.add_argument("--expired-sample",type=int,default=6)
    ap.add_argument("--output-root",default=str(OUTPUT_ROOT))
    a=ap.parse_args()

    pa=latest_dir(PHASE_A_ROOT,["security_master_current.csv","local_cache_recent_coverage.csv"])
    pb1=latest_dir(PHASE_B1_ROOT,["expired_contract_master_after.csv"])
    master=read_csv(pa/"security_master_current.csv")
    cache=read_csv(pa/"local_cache_recent_coverage.csv")
    cm={r["symbol"]:r for r in cache}
    expired=read_csv(pb1/"expired_contract_master_after.csv")

    eligible=[];excluded=[]
    for r in master:
        ok,reason=current_reason(r,cm.get(r["symbol"]))
        rr=dict(r);rr["eligible_research_trade"]=ok;rr["eligibility_reason"]=reason
        (eligible if ok else excluded).append(rr)

    stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    out=Path(a.output_root)/stamp;out.mkdir(parents=True,exist_ok=False)
    write_csv(out/"eligible_current_universe.csv",eligible)
    write_csv(out/"excluded_current_universe.csv",excluded)

    cs=pick_current(eligible,max(1,a.current_sample))
    for r in cs:
        r["sample_role"]="CURRENT_ELIGIBLE";r["eligibility_reason"]="ELIGIBLE_CURRENT_NON_ST_LISTED_SH_SZ"
    es=pick_expired(expired,max(0,a.expired_sample))
    sample=cs+es
    write_csv(out/"pilot_symbols.csv",sample)

    print("="*100)
    print("POSITIONX FULL MARKET DAILY V1 — PHASE B2.1 REFINED PILOT")
    print("="*100)
    print(f"RAW CURRENT={len(master)} ELIGIBLE={len(eligible)}")
    print("EXCLUDED:",dict(Counter(r["eligibility_reason"] for r in excluded)))
    print(f"PILOT current={len(cs)} expired_archive={len(es)}")

    from xtquant import xtdata
    try:xtdata.enable_hello=False
    except Exception:pass

    fields=["open","high","low","close","volume","amount","preClose","suspendFlag"]
    audits=[];allrows=[]
    for i,meta in enumerate(sample,1):
        sym=meta["symbol"];start=meta.get("open_date") or "19900101"
        print(f"[{i}/{len(sample)}] {sym} {meta['sample_role']} start={start}")
        ok,err=download_one(xtdata,sym,start)
        rows=[];read_err=None
        if ok:
            try:rows=collect(xtdata,sym,fields)
            except Exception as e:read_err=f"{type(e).__name__}: {e}"
        ar=validate(meta,rows);ar["download_ok"]=ok;ar["download_error"]=err;ar["read_error"]=read_err
        audits.append(ar)
        for r in rows:
            r["sample_role"]=meta["sample_role"];r["board"]=meta["board"];allrows.append(r)

    write_csv(out/"pilot_symbol_audit.csv",audits)
    df=pd.DataFrame(allrows)
    if not df.empty:df=df.sort_values(["symbol","trade_date"]).reset_index(drop=True)
    df.to_csv(out/"daily_bars_pilot.csv",index=False,encoding="utf-8-sig")
    pq=parquet_test(df,out/"daily_bars_pilot.parquet")

    good=[r for r in audits if r["download_ok"] and not r["read_error"] and r["bars"]>0
          and r["duplicate_dates"]==0 and r["bad_ohlc_rows"]==0 and r["zero_or_negative_close_rows"]==0
          and r["amount_coverage"]==1 and r["preclose_coverage"]==1 and r["suspendflag_coverage"]==1]
    cg=sum(r["sample_role"]=="CURRENT_ELIGIBLE" for r in good)
    eg=sum(r["sample_role"]=="EXPIRED_ARCHIVE_PROBE" for r in good)
    verdict="PASS_FULL_HISTORY_PILOT_REFINED" if cg==len(cs) and eg==len(es) and pq["ok"] else "REVIEW_FULL_HISTORY_PILOT_REFINED"
    rc=0 if verdict.startswith("PASS") else 3

    summary={
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "scope_policy":{
            "allowed_boards":sorted(ALLOWED_BOARDS),
            "exclude_bse":True,
            "exclude_current_st":True,
            "exclude_no_daily_history_current":True,
            "historical_st_policy":"PIT_ST_FILTER_REQUIRED_BEFORE_STRATEGY_RESEARCH",
        },
        "phase_a_dir":str(pa),"phase_b1_dir":str(pb1),
        "current_universe":{
            "raw":len(master),"eligible":len(eligible),
            "excluded_counts":dict(Counter(r["eligibility_reason"] for r in excluded)),
            "eligible_board_counts":dict(Counter(r["board"] for r in eligible)),
        },
        "pilot":{
            "current_sample":len(cs),"expired_sample":len(es),"total_symbols":len(sample),
            "good_current":cg,"good_expired":eg,"total_rows":len(allrows),"parquet":pq,
        },
        "verdict":verdict,"output_dir":str(out)
    }
    write_json(out/"phase_b2_1_summary.json",summary)
    (out/"phase_b2_1_report.md").write_text(
        "# PositionX · Full Market Daily Data V1 — Phase B2.1\n\n"
        f"- Verdict: **{verdict}**\n"
        f"- Current raw universe: **{len(master)}**\n"
        f"- Eligible current universe: **{len(eligible)}**\n"
        f"- Excluded: `{json.dumps(summary['current_universe']['excluded_counts'],ensure_ascii=False)}`\n"
        f"- Pilot current good: **{cg}/{len(cs)}**\n"
        f"- Pilot expired archive good: **{eg}/{len(es)}**\n"
        f"- Parquet: **{pq['ok']}**\n\n"
        "## Scope\n"
        "- BSE excluded.\n- Current ST excluded.\n- Unsupported SH/SZ boards excluded.\n"
        "- Current entries with zero local daily history excluded from current research/trading universe.\n"
        "- Historical ST still requires PIT filtering at research time.\n",
        encoding="utf-8")
    print(json.dumps({"verdict":verdict,"eligible":len(eligible),
                      "excluded":summary["current_universe"]["excluded_counts"],
                      "good_current":f"{cg}/{len(cs)}","good_expired":f"{eg}/{len(es)}",
                      "parquet_ok":pq["ok"]},ensure_ascii=False,indent=2))
    print("SUMMARY:",out/"phase_b2_1_summary.json")
    print("REPORT :",out/"phase_b2_1_report.md")
    return rc

if __name__=="__main__":
    raise SystemExit(main())
