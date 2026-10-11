
from __future__ import annotations
import argparse,difflib,hashlib,json,re,time,urllib.parse,urllib.request
from datetime import datetime,timedelta
from pathlib import Path
import pandas as pd,yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/data/commercial_catalyst_residual_adjudication_v0_2.yaml"

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

def first_col(df,names):
    low={str(c).lower():c for c in df.columns}
    for n in names:
        if n.lower() in low:return low[n.lower()]
    return None

def request_bytes(url,headers=None,data=None,timeout=35,retries=6,backoff=1.0):
    last=None
    headers=headers or {"User-Agent":"Mozilla/5.0"}
    for i in range(retries):
        try:
            req=urllib.request.Request(url,data=data,headers=headers,method="POST" if data is not None else "GET")
            with urllib.request.urlopen(req,timeout=timeout) as r:
                return r.read(),r.headers.get("Content-Type","")
        except Exception as e:
            last=e;time.sleep(backoff*(i+1))
    raise RuntimeError(repr(last))

def request_json(url,headers=None,data=None,**kw):
    raw,_=request_bytes(url,headers,data,**kw)
    s=raw.decode("utf-8","replace").strip()
    if s.startswith("{") or s.startswith("["):return json.loads(s)
    a,b=s.find("("),s.rfind(")")
    if a>=0 and b>a:return json.loads(s[a+1:b])
    p,q=s.find("{"),s.rfind("}")
    if p>=0 and q>p:return json.loads(s[p:q+1])
    raise RuntimeError("not JSON/JSONP")

def exchange_dates(date_s,window):
    d=pd.Timestamp(date_s).date()
    return [(d+timedelta(days=i)).isoformat() for i in range(-window,window+1)]

def sse_rows(cfg,code,start_s,end_s):
    params={
        "isPagination":"true","pageHelp.pageSize":str(cfg["search"]["page_size"]),
        "pageHelp.pageNo":"1","pageHelp.beginPage":"1","pageHelp.cacheSize":"1","pageHelp.endPage":"1",
        "productId":code,"securityType":"0101,120100,020100,020200,120200",
        "reportType":"","reportType2":"","beginDate":start_s,"endDate":end_s,
        "_":str(int(time.time()*1000))
    }
    url=cfg["official"]["sse_api_url"]+"?"+urllib.parse.urlencode(params)
    obj=request_json(url,{
        "User-Agent":"Mozilla/5.0","Referer":cfg["official"]["sse_referer"],
        "Accept":"application/json,text/javascript,*/*;q=0.01"
    },timeout=int(cfg["search"]["timeout_seconds"]),retries=int(cfg["search"]["retries"]),backoff=float(cfg["search"]["backoff_seconds"]))
    rows=[]
    if isinstance(obj,dict):
        if isinstance(obj.get("result"),list):rows+=obj["result"]
        ph=obj.get("pageHelp")
        if isinstance(ph,dict) and isinstance(ph.get("data"),list):rows+=ph["data"]
    return rows

def szse_rows(cfg,code,start_s,end_s):
    payload={"seDate":[start_s,end_s],"searchKey":[],"channelCode":[cfg["official"]["szse_channel_code"]],
             "pageSize":int(cfg["search"]["page_size"]),"pageNum":1,"stock":[code]}
    headers={"User-Agent":"Mozilla/5.0","Referer":cfg["official"]["szse_referer"],"Origin":"https://www.szse.cn",
             "Content-Type":"application/json","Accept":"application/json,text/javascript,*/*;q=0.01",
             "X-Request-Type":"ajax","X-Requested-With":"XMLHttpRequest"}
    obj=request_json(cfg["official"]["szse_api_url"],headers,json.dumps(payload).encode(),
                     timeout=int(cfg["search"]["timeout_seconds"]),retries=int(cfg["search"]["retries"]),backoff=float(cfg["search"]["backoff_seconds"]))
    rows=list(obj.get("data") or []) if isinstance(obj,dict) else []
    count=int(obj.get("announceCount") or len(rows)) if isinstance(obj,dict) else len(rows)
    pages=(count+int(cfg["search"]["page_size"])-1)//int(cfg["search"]["page_size"])
    for p in range(2,pages+1):
        payload["pageNum"]=p;time.sleep(.25)
        z=request_json(cfg["official"]["szse_api_url"],headers,json.dumps(payload).encode(),
                       timeout=int(cfg["search"]["timeout_seconds"]),retries=int(cfg["search"]["retries"]),backoff=float(cfg["search"]["backoff_seconds"]))
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

def unresolved_rows(path):
    df=pd.read_csv(path)
    if "official_exchange_verified" not in df.columns:raise RuntimeError("missing official_exchange_verified")
    ok=df["official_exchange_verified"].astype(str).str.lower().isin(["true","1"])
    err=df["error"].fillna("").astype(str).str.strip()
    return df[(~ok)&err.eq("")].copy()

def discovery_map(cfg):
    maps={}
    for path in [cfg["input"]["eva_events"],cfg["input"]["eva_raw_index"]]:
        p=Path(path)
        if not p.exists():continue
        df=pd.read_parquet(p)
        ann=first_col(df,["announcement_id","announcementId"])
        if ann is None:continue
        src=first_col(df,["source_ref","source_url","pdf_url","adjunctUrl","url"])
        sha=first_col(df,["source_sha256","pdf_sha256","sha256"])
        rawp=first_col(df,["raw_path","pdf_path","source_path"])
        for _,r in df.iterrows():
            aid=safe(r[ann]).strip()
            if not aid:continue
            d=maps.setdefault(aid,{})
            if src and safe(r[src]):d.setdefault("source_ref",safe(r[src]).strip())
            if sha and safe(r[sha]):d.setdefault("eva_sha256",safe(r[sha]).strip())
            if rawp and safe(r[rawp]):d.setdefault("eva_raw_path",safe(r[rawp]).strip())
    return maps

def official_url(ref,base):
    r=safe(ref).strip()
    if not r:return None
    if r.startswith("http://") or r.startswith("https://"):return r
    return base+r.lstrip("/")

def allowed_host(url,hosts):
    try:return urllib.parse.urlparse(url).hostname in set(hosts)
    except Exception:return False

def download_official_pdf(cfg,url,outfile):
    if not allowed_host(url,cfg["official"]["allowed_cninfo_hosts"]):
        raise RuntimeError(f"non-official host: {url}")
    raw,ctype=request_bytes(url,{"User-Agent":"Mozilla/5.0","Referer":"https://www.cninfo.com.cn/"},
                            timeout=int(cfg["search"]["timeout_seconds"]),retries=int(cfg["search"]["retries"]),backoff=float(cfg["search"]["backoff_seconds"]))
    pdf=raw.startswith(b"%PDF-") or "pdf" in ctype.lower()
    if not pdf:raise RuntimeError(f"official URL did not return PDF; content-type={ctype}")
    outfile.parent.mkdir(parents=True,exist_ok=True);outfile.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest(),len(raw),ctype

def check(cfg):
    ps=[Path(cfg["input"]["exchange_detail"]),Path(cfg["input"]["eva_events"]),Path(cfg["input"]["eva_raw_index"])]
    ok=all(p.exists() for p in ps)
    print(json.dumps({"status":"PASS_COMMERCIAL_RESIDUAL_ADJUDICATION_V0_2_CHECK" if ok else "BLOCKED_COMMERCIAL_RESIDUAL_ADJUDICATION_V0_2_CHECK",
                      "inputs":[{"path":str(p),"exists":p.exists()} for p in ps],
                      "research_domain":"SH/SZ only","eva_role":"discovery pointer only","official_source_required":True},ensure_ascii=False,indent=2))
    return 0 if ok else 3

def run(cfg):
    rows=unresolved_rows(cfg["input"]["exchange_detail"])
    dmap=discovery_map(cfg)
    outdir=Path(cfg["output_dir"]);pdfdir=Path(cfg["official_pdf_dir"]);outdir.mkdir(parents=True,exist_ok=True)
    results=[]
    window=int(cfg["search"]["exchange_day_window"]);thr=float(cfg["search"]["fuzzy_title_threshold"])
    for _,r in rows.iterrows():
        aid=safe(r["announcement_id"]).strip();sym=safe(r["canonical_symbol"]);code=code6(sym)
        ds=str(r["event_date"])[:10];dates=exchange_dates(ds,window);start_s,end_s=dates[0],dates[-1]
        ex="SH" if sym.upper().endswith(".SH") else "SZ"
        exch_verified=False;bm=None;exerr=None
        try:
            erows=sse_rows(cfg,code,start_s,end_s) if ex=="SH" else szse_rows(cfg,code,start_s,end_s)
            bm=best_match(r["target_title"],erows);exch_verified=bool(bm and bm["ratio"]>=thr)
        except Exception as e:
            erows=[];exerr=f"{type(e).__name__}: {e}"

        pointer=dmap.get(aid,{})
        url=official_url(pointer.get("source_ref"),cfg["official"]["cninfo_static_base"])
        pdf_verified=False;pdf_sha=None;pdf_size=None;pdf_err=None
        if not exch_verified and url:
            try:
                pdf_sha,pdf_size,_=download_official_pdf(cfg,url,pdfdir/f"{aid}.pdf")
                pdf_verified=True
            except Exception as e:
                pdf_err=f"{type(e).__name__}: {e}"

        accepted=exch_verified or pdf_verified
        basis="SSE_SZSE_WINDOW_VERIFIED" if exch_verified else ("CNINFO_OFFICIAL_PDF_REFETCH_VERIFIED" if pdf_verified else "UNRESOLVED")
        results.append({
            "announcement_id":aid,"canonical_symbol":sym,"event_date":ds,"target_title":r["target_title"],
            "exchange":ex,"exchange_window_start":start_s,"exchange_window_end":end_s,
            "exchange_rows":len(erows),"best_exchange_title":bm["title"] if bm else None,
            "exchange_title_ratio":bm["ratio"] if bm else None,"exchange_verified":exch_verified,
            "eva_discovery_source_ref":pointer.get("source_ref"),"official_cninfo_url":url,
            "official_cninfo_pdf_verified":pdf_verified,"official_pdf_sha256":pdf_sha,"official_pdf_bytes":pdf_size,
            "eva_recorded_sha256":pointer.get("eva_sha256"),
            "independently_officially_verified":accepted,"verification_basis":basis,
            "exchange_error":exerr,"pdf_error":pdf_err,
        })
        print(aid,sym,basis)
    rdf=pd.DataFrame(results)
    rdf.to_csv(outdir/"commercial_residual_adjudication_detail_v0_2.csv",index=False,encoding="utf-8-sig")
    total=len(rdf);verified=int(rdf["independently_officially_verified"].sum()) if total else 0
    exchange_n=int(rdf["exchange_verified"].sum()) if total else 0
    pdf_n=int(rdf["official_cninfo_pdf_verified"].sum()) if total else 0
    unresolved=total-verified
    result={
        "status":"PASS_COMMERCIAL_CATALYST_RESIDUAL_ADJUDICATION_V0_2_RUN",
        "generated_at":datetime.now().isoformat(timespec="seconds"),"audit_id":cfg["audit_id"],
        "research_domain":"SH/SZ A-shares only","input_residual_rows":total,
        "results":{"officially_verified_total":verified,"exchange_window_verified":exchange_n,
                   "cninfo_official_pdf_refetch_verified":pdf_n,"unresolved":unresolved,
                   "official_verification_rate":verified/total if total else None},
        "decision":{
            "if_all_verified":"Current residual Eva gap rows may be used only after official-source refetch; Eva remains Layer 4 discovery, not truth.",
            "if_unresolved":"Leave unresolved rows excluded. Do not force classification or import them.",
            "keyword_expansion_primary":False,
            "pdf_bulk_download_now":False
        },
        "artifact":str(outdir/"commercial_residual_adjudication_detail_v0_2.csv"),"safety":cfg["safety"]}
    (outdir/"commercial_catalyst_residual_adjudication_v0_2_summary.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2));print("OUTPUT:",outdir)
    return 0

def main():
    p=argparse.ArgumentParser();p.add_argument("command",choices=["check","run"]);a=p.parse_args();cfg=load_cfg()
    try:raise SystemExit(check(cfg) if a.command=="check" else run(cfg))
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_COMMERCIAL_RESIDUAL_ADJUDICATION_V0_2","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2));raise SystemExit(3)
if __name__=="__main__":main()
