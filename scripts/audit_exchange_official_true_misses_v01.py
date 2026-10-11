
from __future__ import annotations
import argparse,difflib,json,re,time,urllib.parse,urllib.request
from datetime import datetime
from pathlib import Path
import pandas as pd,yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/data/exchange_official_true_miss_audit_v0_1.yaml"

def load_cfg(): return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def safe(v):
    if v is None:return ""
    try:
        if pd.isna(v):return ""
    except Exception:pass
    return str(v)

def norm_title(v):
    s=re.sub(r"</?em>","",safe(v),flags=re.I)
    s=re.sub(r"[《》【】\[\]（）()：:，,。．·\s]+","",s)
    s=re.sub(r"^关于","",s)
    return s.strip()

def code6(v):
    m=re.search(r"(\d{6})",safe(v))
    return m.group(1) if m else None

def exchange(v):
    s=safe(v).upper()
    if s.endswith(".SH"):return "SH"
    if s.endswith(".SZ"):return "SZ"
    return None

def request_json(url,headers,data=None,timeout=30,retries=6,backoff=1.0):
    last=None
    for i in range(retries):
        try:
            req=urllib.request.Request(url,data=data,headers=headers,method="POST" if data is not None else "GET")
            with urllib.request.urlopen(req,timeout=timeout) as r:
                raw=r.read().decode("utf-8","replace").strip()
            if raw.startswith("{") or raw.startswith("["): return json.loads(raw)
            a,b=raw.find("("),raw.rfind(")")
            if a>=0 and b>a:return json.loads(raw[a+1:b])
            p,q=raw.find("{"),raw.rfind("}")
            if p>=0 and q>p:return json.loads(raw[p:q+1])
            raise RuntimeError("not JSON/JSONP")
        except Exception as e:
            last=e;time.sleep(backoff*(i+1))
    raise RuntimeError(repr(last))

def sse_rows(cfg,code,date_s):
    params={
        "isPagination":"true","pageHelp.pageSize":str(cfg["sse"]["page_size"]),
        "pageHelp.pageNo":"1","pageHelp.beginPage":"1","pageHelp.cacheSize":"1","pageHelp.endPage":"1",
        "productId":code,"securityType":cfg["sse"]["security_type"],"reportType":"","reportType2":"",
        "beginDate":date_s,"endDate":date_s,"_":str(int(time.time()*1000))
    }
    url=cfg["sse"]["api_url"]+"?"+urllib.parse.urlencode(params)
    obj=request_json(url,{
        "User-Agent":"Mozilla/5.0","Referer":cfg["sse"]["referer"],
        "Accept":"application/json,text/javascript,*/*;q=0.01"
    },timeout=int(cfg["network"]["timeout_seconds"]),retries=int(cfg["network"]["retries"]),backoff=float(cfg["network"]["backoff_seconds"]))
    rows=[]
    if isinstance(obj,dict):
        if isinstance(obj.get("result"),list):rows+=obj["result"]
        ph=obj.get("pageHelp")
        if isinstance(ph,dict) and isinstance(ph.get("data"),list):rows+=ph["data"]
    return rows

def szse_rows(cfg,code,date_s):
    payload={"seDate":[date_s,date_s],"searchKey":[],"channelCode":[cfg["szse"]["channel_code"]],
             "pageSize":int(cfg["szse"]["page_size"]),"pageNum":1,"stock":[code]}
    headers={"User-Agent":"Mozilla/5.0","Referer":cfg["szse"]["referer"],"Origin":"https://www.szse.cn",
             "Content-Type":"application/json","Accept":"application/json,text/javascript,*/*;q=0.01",
             "X-Request-Type":"ajax","X-Requested-With":"XMLHttpRequest"}
    obj=request_json(cfg["szse"]["api_url"],headers,json.dumps(payload).encode(),
                     int(cfg["network"]["timeout_seconds"]),int(cfg["network"]["retries"]),float(cfg["network"]["backoff_seconds"]))
    rows=list(obj.get("data") or []) if isinstance(obj,dict) else []
    count=int(obj.get("announceCount") or len(rows)) if isinstance(obj,dict) else len(rows)
    pages=(count+int(cfg["szse"]["page_size"])-1)//int(cfg["szse"]["page_size"])
    for p in range(2,pages+1):
        payload["pageNum"]=p;time.sleep(.25)
        z=request_json(cfg["szse"]["api_url"],headers,json.dumps(payload).encode(),
                       int(cfg["network"]["timeout_seconds"]),int(cfg["network"]["retries"]),float(cfg["network"]["backoff_seconds"]))
        rows+=list(z.get("data") or [])
    return rows

def extract_title(row):
    for k in ["title","TITLE","bulletinTitle","announcementTitle"]:
        if safe(row.get(k)):return safe(row.get(k))
    for k,v in row.items():
        if "title" in str(k).lower() and safe(v):return safe(v)
    return ""

def best_match(target,rows):
    tn=norm_title(target);best=None;br=-1
    for r in rows:
        rt=extract_title(r);rn=norm_title(rt)
        if not rn:continue
        ratio=1.0 if rn==tn else difflib.SequenceMatcher(None,tn,rn).ratio()
        if ratio>br:br=ratio;best={"title":rt,"ratio":ratio}
    return best

def load_true_misses(path):
    df=pd.read_csv(path)
    req={"announcement_id","canonical_symbol","event_date","title","query_completed","recovered_by_blank_exact_date"}
    if req-set(df.columns):raise RuntimeError(f"missing {sorted(req-set(df.columns))}")
    qc=df["query_completed"].astype(str).str.lower().isin(["true","1"])
    rr=df["recovered_by_blank_exact_date"].astype(str).str.lower().isin(["true","1"])
    z=df[qc & ~rr].copy();z["exchange"]=z["canonical_symbol"].map(exchange)
    return z[z["exchange"].isin(["SH","SZ"])].copy()

def check(cfg):
    p=Path(cfg["input"]["pilot_detail"]);ok=p.exists()
    print(json.dumps({"status":"PASS_EXCHANGE_OFFICIAL_TRUE_MISS_AUDIT_V0_1_CHECK" if ok else "BLOCKED_EXCHANGE_OFFICIAL_TRUE_MISS_AUDIT_V0_1_CHECK",
                      "pilot_detail":str(p),"exists":ok,"first_party_only":True,"pdf_download":False},ensure_ascii=False,indent=2))
    return 0 if ok else 3

def run(cfg):
    misses=load_true_misses(cfg["input"]["pilot_detail"])
    outdir=Path(cfg["output_dir"]);outdir.mkdir(parents=True,exist_ok=True)
    res=[]
    for _,r in misses.iterrows():
        code=code6(r["canonical_symbol"]);ds=str(r["event_date"])[:10];ex=r["exchange"]
        try:
            rows=sse_rows(cfg,code,ds) if ex=="SH" else szse_rows(cfg,code,ds)
            bm=best_match(r["title"],rows);thr=float(cfg["matching"]["fuzzy_title_threshold"])
            verified=bool(bm and bm["ratio"]>=thr)
            res.append({"announcement_id":r["announcement_id"],"canonical_symbol":r["canonical_symbol"],"event_date":ds,
                        "target_title":r["title"],"exchange":ex,"official_rows_returned":len(rows),
                        "best_exchange_title":bm["title"] if bm else None,"title_match_ratio":bm["ratio"] if bm else None,
                        "official_exchange_verified":verified,"error":None})
            print(ex,code,ds,"rows",len(rows),"verified",verified,"ratio",bm["ratio"] if bm else None)
        except Exception as e:
            res.append({"announcement_id":r["announcement_id"],"canonical_symbol":r["canonical_symbol"],"event_date":ds,
                        "target_title":r["title"],"exchange":ex,"official_rows_returned":None,"best_exchange_title":None,
                        "title_match_ratio":None,"official_exchange_verified":False,"error":f"{type(e).__name__}: {e}"})
    rdf=pd.DataFrame(res)
    rdf.to_csv(outdir/"exchange_official_true_miss_detail_v0_1.csv",index=False,encoding="utf-8-sig")
    total=len(rdf);errors=int(rdf["error"].notna().sum()) if total else 0
    verified=int(rdf["official_exchange_verified"].sum()) if total else 0
    result={"status":"PASS_EXCHANGE_OFFICIAL_TRUE_MISS_AUDIT_V0_1_RUN" if errors==0 else "PARTIAL_EXCHANGE_OFFICIAL_TRUE_MISS_AUDIT_V0_1_RUN",
            "generated_at":datetime.now().isoformat(timespec="seconds"),"audit_id":cfg["audit_id"],
            "research_domain":"SH/SZ A-shares only","input_true_misses":total,
            "results":{"official_exchange_verified":verified,"official_exchange_unverified":total-verified-errors,
                       "network_or_parser_errors":errors,"verification_rate_among_completed":verified/(total-errors) if total-errors else None},
            "decision":{"cninfo_blank_query_as_sole_source":False,
                        "recommended_architecture":"CNINFO broad metadata primary + direct SSE/SZSE official supplement for coverage-sensitive event research.",
                        "keyword_expansion_primary":False,"eva_auto_import":False},
            "artifact":str(outdir/"exchange_official_true_miss_detail_v0_1.csv"),"safety":cfg["safety"]}
    (outdir/"exchange_official_true_miss_audit_v0_1_summary.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2));print("OUTPUT:",outdir)
    return 0 if errors==0 else 3

def main():
    p=argparse.ArgumentParser();p.add_argument("command",choices=["check","run"]);a=p.parse_args();cfg=load_cfg()
    try:raise SystemExit(check(cfg) if a.command=="check" else run(cfg))
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_EXCHANGE_OFFICIAL_TRUE_MISS_AUDIT_V0_1","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        raise SystemExit(3)

if __name__=="__main__":main()
