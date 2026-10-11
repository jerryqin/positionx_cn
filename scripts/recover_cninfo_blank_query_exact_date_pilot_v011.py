
from __future__ import annotations
import argparse, gzip, json, time, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path
import pandas as pd, yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/data/cninfo_blank_query_exact_date_pilot_v0_1_1.yaml"

def load_cfg(): return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def safe_text(v):
    if v is None: return ""
    try:
        if pd.isna(v): return ""
    except Exception: pass
    return str(v)

def find_col(df,names):
    low={str(c).lower():c for c in df.columns}
    for n in names:
        if n.lower() in low: return low[n.lower()]
    return None

def headers(cfg):
    return {
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36",
        "Content-Type":"application/x-www-form-urlencoded; charset=UTF-8",
        "Referer":cfg["cninfo"]["referer"],
        "X-Requested-With":"XMLHttpRequest",
        "Accept":"application/json, text/javascript, */*; q=0.01",
        "Connection":"close",
    }

def body(cfg,date_s,column,plate,page):
    return urllib.parse.urlencode({
        "pageNum":page,"pageSize":cfg["cninfo"]["page_size"],"column":column,
        "tabName":"fulltext","plate":plate,"stock":"","searchkey":"","secid":"",
        "category":"","trade":"","seDate":f"{date_s}~{date_s}",
        "sortName":"","sortType":"","isHLtitle":"true",
    }).encode("utf-8")

def fetch_page(cfg,date_s,column,plate,page):
    last=None
    for i in range(int(cfg["cninfo"]["attempts_per_page"])):
        urls=list(cfg["cninfo"]["api_urls"])
        if i%2: urls.reverse()
        for url in urls:
            try:
                req=urllib.request.Request(url,data=body(cfg,date_s,column,plate,page),method="POST",headers=headers(cfg))
                with urllib.request.urlopen(req,timeout=int(cfg["cninfo"]["timeout_seconds"])) as r:
                    obj=json.loads(r.read().decode("utf-8","replace"))
                if not isinstance(obj,dict): raise RuntimeError("response not json object")
                return obj,url,i+1
            except Exception as e:
                last=e
        time.sleep(min(float(cfg["cninfo"]["max_backoff_seconds"]),
                       float(cfg["cninfo"]["initial_backoff_seconds"])*(2**min(i,4))))
    raise RuntimeError(f"failed date={date_s} {column}/{plate} page={page}: {last!r}")

def key(date_s,column): return f"{date_s}__{column}"

def old_raw(cfg,date_s,column):
    return Path(cfg["input"]["prior_raw_dir"])/f"{key(date_s,column)}.json.gz"

def page_path(outdir,date_s,column,page):
    d=outdir/"page_cache"/key(date_s,column); d.mkdir(parents=True,exist_ok=True)
    return d/f"page_{page:04d}.json.gz"

def save_page(path,obj,url,tries):
    with gzip.open(path,"wt",encoding="utf-8") as f:
        json.dump({"payload":obj,"api_url":url,"attempts_used":tries},f,ensure_ascii=False)

def read_page(path):
    with gzip.open(path,"rt",encoding="utf-8") as f:
        return json.load(f)["payload"]

def recover_query(cfg,outdir,date_s,column,plate):
    p=old_raw(cfg,date_s,column)
    if p.exists():
        with gzip.open(p,"rt",encoding="utf-8") as f: old=json.load(f)
        return old.get("rows",[]), int(old.get("pages") or 0), "REUSE_V0_1_SUCCESS"

    p1=page_path(outdir,date_s,column,1)
    if p1.exists(): first=read_page(p1)
    else:
        first,url,n=fetch_page(cfg,date_s,column,plate,1); save_page(p1,first,url,n)
    total=int(first.get("totalpages") or first.get("totalPages") or 0)
    payloads=[first]
    for pg in range(2,total+1):
        pp=page_path(outdir,date_s,column,pg)
        if pp.exists(): obj=read_page(pp)
        else:
            time.sleep(float(cfg["cninfo"]["inter_page_delay_seconds"]))
            obj,url,n=fetch_page(cfg,date_s,column,plate,pg); save_page(pp,obj,url,n)
        payloads.append(obj)
    rows=[]
    for pno,obj in enumerate(payloads,1):
        for r in obj.get("announcements") or []:
            z=dict(r); z["_page"]=pno; rows.append(z)
    return rows,total,"RECOVERED_V0_1_FAILURE"

def check(cfg):
    ps=[Path(cfg["input"]["prior_summary"]),Path(cfg["input"]["prior_detail"]),Path(cfg["input"]["prior_raw_dir"])]
    ok=all(p.exists() for p in ps)
    print(json.dumps({
        "status":"PASS_CNINFO_BLANK_QUERY_PILOT_V0_1_1_CHECK" if ok else "BLOCKED_CNINFO_BLANK_QUERY_PILOT_V0_1_1_CHECK",
        "inputs":[{"path":str(p),"exists":p.exists()} for p in ps],
        "research_logic_changed":False,"query_logic_changed":False,"keyword_expansion":False
    },ensure_ascii=False,indent=2))
    return 0 if ok else 3

def run(cfg):
    prior=json.loads(Path(cfg["input"]["prior_summary"]).read_text(encoding="utf-8"))
    detail=pd.read_csv(cfg["input"]["prior_detail"])
    req={"announcement_id","canonical_symbol","event_date","cohort"}
    if req-set(detail.columns): raise RuntimeError(f"missing cols {sorted(req-set(detail.columns))}")
    detail["column"]=detail["canonical_symbol"].map(lambda s:"sse" if safe_text(s).upper().endswith(".SH") else ("szse" if safe_text(s).upper().endswith(".SZ") else None))
    detail["plate"]=detail["column"].map({"sse":"sh","szse":"sz"})
    outdir=Path(cfg["output_dir"]); outdir.mkdir(parents=True,exist_ok=True)
    groups=detail[["event_date","column","plate"]].drop_duplicates().sort_values(["event_date","column"])
    all_rows=[]; done=set(); failures=[]
    for _,g in groups.iterrows():
        ds,col,pl=str(g["event_date"]),str(g["column"]),str(g["plate"])
        try:
            rows,pages,status=recover_query(cfg,outdir,ds,col,pl)
            print(f"{status:>24} {ds} {col}/{pl} pages={pages} rows={len(rows)}")
            done.add((ds,col))
            for r in rows:
                z=dict(r); z["_query_event_date"]=ds; z["_query_column"]=col; all_rows.append(z)
        except Exception as e:
            failures.append({"event_date":ds,"column":col,"plate":pl,"error":f"{type(e).__name__}: {e}"})
            print("FAILED",failures[-1])

    fetched=pd.DataFrame(all_rows)
    idcol=find_col(fetched,["announcementId","announcement_id"]) if len(fetched) else None
    fids=set(fetched[idcol].astype("string").str.strip()) if idcol else set()
    detail["query_completed"]=detail.apply(lambda r:(str(r["event_date"]),r["column"]) in done,axis=1)
    detail["recovered_by_blank_exact_date"]=detail["announcement_id"].astype("string").isin(fids)

    threshold=float(cfg["decision"]["recovery_min_each_cohort"])
    by={}
    for cohort,g in detail.groupby("cohort"):
        comp=g[g["query_completed"]]
        by[cohort]={
            "sample_rows":len(g),
            "completed_query_rows":len(comp),
            "network_inconclusive_rows":len(g)-len(comp),
            "recovered_on_completed_queries":int(comp["recovered_by_blank_exact_date"].sum()),
            "recovery_rate_completed_queries":float(comp["recovered_by_blank_exact_date"].mean()) if len(comp) else None,
            "true_retrieval_misses_on_completed_queries":int((~comp["recovered_by_blank_exact_date"]).sum()) if len(comp) else None,
        }

    complete=(len(failures)==0 and all(v["network_inconclusive_rows"]==0 for v in by.values()))
    rates=[v["recovery_rate_completed_queries"] for v in by.values() if v["recovery_rate_completed_queries"] is not None]
    viable=(all(r>=threshold for r in rates) if complete else None)
    state=("VIABLE" if viable else "NOT_VIABLE_AT_95PCT") if complete else "INCONCLUSIVE_NETWORK"

    detail.to_csv(outdir/"blank_query_pilot_detail_v0_1_1.csv",index=False,encoding="utf-8-sig")
    result={
        "status":"PASS_CNINFO_BLANK_QUERY_PILOT_V0_1_1_RUN" if complete else "PARTIAL_CNINFO_BLANK_QUERY_PILOT_V0_1_1_RUN",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "pilot_id":cfg["pilot_id"],
        "prior_v0_1":{"status":prior.get("status"),"failed_queries":prior.get("network",{}).get("failed_queries"),
                     "note":"V0.1 viability=false was network-contaminated."},
        "recovery":{"query_groups":len(groups),"completed_query_groups":len(done),"failed_query_groups":len(failures),
                    "failures":failures[:20],"by_cohort":by},
        "decision":{"threshold_each_cohort":threshold,"state":state,
                    "broad_first_party_metadata_viable":viable,
                    "if_viable":"Build reusable SH/SZ all-announcement CNINFO metadata; local filter/classify.",
                    "if_not_viable":"Use direct SSE/SZSE official metadata as Layer-2 supplements.",
                    "if_inconclusive":"Rerun this same recovery command; cached pages are reused."},
        "safety":cfg["safety"],
    }
    (outdir/"cninfo_blank_query_exact_date_pilot_v0_1_1_summary.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2)); print(f"OUTPUT: {outdir}")
    return 0 if complete else 3

def main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=["check","run"])
    a=p.parse_args(); cfg=load_cfg()
    try: raise SystemExit(check(cfg) if a.command=="check" else run(cfg))
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_CNINFO_BLANK_QUERY_PILOT_V0_1_1","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        raise SystemExit(3)

if __name__=="__main__": main()
