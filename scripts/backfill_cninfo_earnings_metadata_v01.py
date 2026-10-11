from __future__ import annotations
import argparse, calendar, hashlib, json, os, re, socket, time, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import duckdb
import pandas as pd

CST = timezone(timedelta(hours=8))
CNINFO_API = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC = "http://static.cninfo.com.cn/"
CNINFO_HDRS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
    "X-Requested-With": "XMLHttpRequest",
}
KEYWORDS = ("业绩预告", "业绩快报")
FORECAST_TITLE_PAT = re.compile(r"业绩预告|业绩预增|业绩预减|预盈|预亏|首亏|续亏|扭亏")
EXPRESS_TITLE_PAT = re.compile(r"业绩快报")
OUT_ROOT = Path(r"D:\positionx_data\events\cninfo_earnings_history_v0_1")
DEFAULT_START = "2024-01-01"
DEFAULT_END = "2026-09-29"

def now_cst():
    return datetime.now(CST)

def iso_now():
    return now_cst().isoformat(timespec="seconds")

def qpath(p: Path) -> str:
    return str(p).replace("\\", "/").replace("'", "''")

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)

def write_parquet(df: pd.DataFrame, path: Path) -> None:
    con = duckdb.connect(":memory:")
    try:
        con.register("_df", df)
        con.execute(f"COPY _df TO '{qpath(path)}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    finally:
        con.close()

def clean_title(v: Any) -> str:
    return re.sub(r"</?em>", "", str(v or "")).strip()

def classify_title(title: Any) -> str | None:
    t = clean_title(title)
    if EXPRESS_TITLE_PAT.search(t):
        return "EXPRESS"
    if FORECAST_TITLE_PAT.search(t):
        return "FORECAST"
    return None

def normalize_event_time(ms: Any):
    s = pd.to_numeric(pd.Series([ms]), errors="coerce").iloc[0]
    if pd.isna(s):
        return None, None
    t = pd.to_datetime([s], unit="ms", utc=True, errors="coerce")[0]
    if pd.isna(t):
        return None, None
    local = t.tz_convert(CST)
    iso = local.strftime("%Y-%m-%d %H:%M:%S")
    return iso, ("DAY" if local.strftime("%H:%M:%S") == "00:00:00" else "SECOND")

def month_chunks(start: date, end: date):
    cur = date(start.year, start.month, 1)
    while cur <= end:
        last = calendar.monthrange(cur.year, cur.month)[1]
        mend = date(cur.year, cur.month, last)
        yield max(start, cur), min(end, mend), f"{cur.year:04d}-{cur.month:02d}"
        cur = date(cur.year + (1 if cur.month == 12 else 0), 1 if cur.month == 12 else cur.month + 1, 1)

def cninfo_post(start_date: str, end_date: str, page_num: int, searchkey: str, tries: int):
    body = urllib.parse.urlencode({
        "pageNum": page_num, "pageSize": 30, "column": "", "tabName": "fulltext",
        "plate": "", "stock": "", "searchkey": searchkey, "secid": "", "category": "",
        "trade": "", "seDate": f"{start_date}~{end_date}", "sortName": "",
        "sortType": "", "isHLtitle": "true",
    }).encode()
    last = None
    for i in range(tries):
        req = urllib.request.Request(CNINFO_API, data=body, method="POST", headers=CNINFO_HDRS)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                obj = json.loads(r.read().decode("utf-8", "replace"))
            if not isinstance(obj, dict):
                raise RuntimeError(f"unexpected response type={type(obj).__name__}")
            return obj
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"CNINFO failed {start_date}~{end_date} {searchkey} page={page_num}: {last!r}")

def normalize_rows(rows, observed_at: str) -> pd.DataFrame:
    cols = ["announcementId","secCode","secName","announcementTitle","announcementTime",
            "storageTime","adjunctUrl","query_keyword","event_time","event_precision",
            "event_class","pdf_url","backfill_observed_at"]
    if not rows:
        return pd.DataFrame(columns=cols)
    out = []
    for r in rows:
        et, precision = normalize_event_time(r.get("announcementTime"))
        title = clean_title(r.get("announcementTitle"))
        out.append({
            "announcementId": str(r.get("announcementId") or ""),
            "secCode": str(r.get("secCode") or ""),
            "secName": str(r.get("secName") or ""),
            "announcementTitle": title,
            "announcementTime": r.get("announcementTime"),
            "storageTime": r.get("storageTime"),
            "adjunctUrl": str(r.get("adjunctUrl") or ""),
            "query_keyword": str(r.get("query_keyword") or ""),
            "event_time": et,
            "event_precision": precision,
            "event_class": classify_title(title),
            "pdf_url": CNINFO_STATIC + str(r.get("adjunctUrl") or "") if r.get("adjunctUrl") else "",
            "backfill_observed_at": observed_at,
        })
    df = pd.DataFrame(out)
    merged = []
    for ann_id, g in df.groupby("announcementId", sort=False, dropna=False):
        rec = g.iloc[0].to_dict()
        rec["query_keyword"] = "|".join(sorted({str(x) for x in g["query_keyword"].dropna() if str(x)}))
        merged.append(rec)
    return pd.DataFrame(merged, columns=cols)

def fetch_chunk(cstart, cend, tmp_dir: Path, delay: float, tries: int, max_pages: int):
    observed_at = iso_now()
    rows, page_manifest = [], []
    for kw in KEYWORDS:
        kw_dir = tmp_dir / "raw_pages" / kw
        kw_dir.mkdir(parents=True, exist_ok=True)
        first = cninfo_post(str(cstart), str(cend), 1, kw, tries)
        total_pages = int(first.get("totalpages") or 0)
        pages = max(total_pages, 1 if (first.get("announcements") or []) else 0)
        if pages > max_pages:
            raise RuntimeError(f"FAIL_CLOSED pages={pages}>{max_pages} chunk={cstart}~{cend} kw={kw}")
        for p in range(1, pages + 1):
            payload = first if p == 1 else cninfo_post(str(cstart), str(cend), p, kw, tries)
            raw_path = kw_dir / f"page_{p:04d}.json"
            raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            anns = payload.get("announcements") or []
            for row in anns:
                z = dict(row); z["query_keyword"] = kw; rows.append(z)
            page_manifest.append({"keyword": kw, "page": p, "announcements": len(anns),
                                  "raw_path": str(raw_path), "sha256": sha256_file(raw_path)})
            if p < pages: time.sleep(delay)
        time.sleep(delay)
    return normalize_rows(rows, observed_at), observed_at, page_manifest

def validate(df, canonical, start, end):
    missing_id = int((df["announcementId"].astype(str).str.len() == 0).sum()) if len(df) else 0
    dup_ids = int(df.duplicated(subset=["announcementId"]).sum()) if len(df) else 0
    missing_time = int(df["event_time"].isna().sum()) if len(df) else 0
    bad_classes = sorted(set(canonical["event_class"].dropna()) - {"FORECAST","EXPRESS"}) if len(canonical) else []
    out_of_range = 0
    if len(df):
        d = pd.to_datetime(df["event_time"], errors="coerce").dt.date
        out_of_range = int(((d < start) | (d > end)).fillna(False).sum())
    ok = missing_id == 0 and dup_ids == 0 and missing_time == 0 and not bad_classes and out_of_range == 0
    return {"ok": ok, "raw_dedup_rows": int(len(df)), "canonical_rows": int(len(canonical)),
            "missing_announcement_id": missing_id, "duplicate_announcement_ids_after_dedup": dup_ids,
            "missing_event_time": missing_time, "out_of_requested_date_range": out_of_range,
            "unexpected_canonical_classes": bad_classes}

def aggregate(run_dir: Path, chunk_dirs):
    raw_paths = [p/"raw_query_matches.parquet" for p in chunk_dirs]
    can_paths = [p/"canonical_events.parquet" for p in chunk_dirs]
    con = duckdb.connect(":memory:")
    try:
        raw_expr = "[" + ",".join(f"'{qpath(p)}'" for p in raw_paths) + "]"
        can_expr = "[" + ",".join(f"'{qpath(p)}'" for p in can_paths) + "]"
        raw_all = run_dir/"raw_query_matches_all.parquet"
        can_all = run_dir/"canonical_events_all.parquet"
        con.execute(f"""COPY (
            SELECT * EXCLUDE(rn) FROM (
                SELECT *, row_number() OVER (PARTITION BY announcementId ORDER BY event_time, backfill_observed_at) rn
                FROM read_parquet({raw_expr})
            ) WHERE rn=1 ORDER BY event_time, announcementId
        ) TO '{qpath(raw_all)}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
        con.execute(f"""COPY (
            SELECT * EXCLUDE(rn) FROM (
                SELECT *, row_number() OVER (PARTITION BY announcementId ORDER BY event_time, backfill_observed_at) rn
                FROM read_parquet({can_expr})
            ) WHERE rn=1 ORDER BY event_time, announcementId
        ) TO '{qpath(can_all)}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
        monthly = con.execute(f"""SELECT
            strftime(try_cast(event_time AS TIMESTAMP), '%Y-%m') AS event_month,
            event_class,
            count(*) AS events,
            count(DISTINCT secCode) AS symbols
            FROM read_parquet('{qpath(can_all)}')
            GROUP BY 1, 2
            ORDER BY 1, 2""").df()
        monthly.to_csv(run_dir/"monthly_summary.csv", index=False, encoding="utf-8-sig")
        cls = con.execute(f"""SELECT event_class, count(*) events, count(DISTINCT announcementId) announcement_ids,
            count(DISTINCT secCode) symbols, min(event_time) min_event_time, max(event_time) max_event_time,
            sum(CASE WHEN event_precision='SECOND' THEN 1 ELSE 0 END) second_precision,
            sum(CASE WHEN event_precision='DAY' THEN 1 ELSE 0 END) day_precision
            FROM read_parquet('{qpath(can_all)}') GROUP BY event_class ORDER BY event_class""").df()
        cls.to_csv(run_dir/"event_class_summary.csv", index=False, encoding="utf-8-sig")
        r = con.execute(f"SELECT count(*),count(DISTINCT announcementId),min(event_time),max(event_time) FROM read_parquet('{qpath(raw_all)}')").fetchone()
        c = con.execute(f"""SELECT count(*),count(DISTINCT announcementId),count(DISTINCT secCode),
            min(event_time),max(event_time),
            sum(CASE WHEN event_precision='SECOND' THEN 1 ELSE 0 END),
            sum(CASE WHEN event_precision='DAY' THEN 1 ELSE 0 END)
            FROM read_parquet('{qpath(can_all)}')""").fetchone()
        return {
            "raw_query_matches": {"rows": int(r[0]), "announcement_ids": int(r[1]), "min_event_time": str(r[2]) if r[2] else None,
                                  "max_event_time": str(r[3]) if r[3] else None, "path": str(raw_all), "sha256": sha256_file(raw_all)},
            "canonical_events": {"rows": int(c[0]), "announcement_ids": int(c[1]), "symbols": int(c[2]),
                                 "min_event_time": str(c[3]) if c[3] else None, "max_event_time": str(c[4]) if c[4] else None,
                                 "second_precision": int(c[5] or 0), "day_precision": int(c[6] or 0),
                                 "path": str(can_all), "sha256": sha256_file(can_all)}
        }
    finally:
        con.close()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--out-root", default=str(OUT_ROOT))
    ap.add_argument("--delay", type=float, default=0.8)
    ap.add_argument("--tries", type=int, default=4)
    ap.add_argument("--max-pages", type=int, default=500)
    ap.add_argument("--run-id", default=None)
    a = ap.parse_args()
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    if end < start: raise RuntimeError("end < start")
    run_id = a.run_id or f"pilot_{start:%Y%m%d}_{end:%Y%m%d}"
    run_dir = Path(a.out_root)/"runs"/run_id
    chunks_root = run_dir/"chunks"; chunks_root.mkdir(parents=True, exist_ok=True)
    committed = []
    started_at = iso_now()

    for cstart, cend, label in month_chunks(start, end):
        final_dir = chunks_root/label
        manifest_path = final_dir/"chunk_manifest.json"
        if manifest_path.exists():
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            if m.get("status") == "SUCCESS":
                print("SKIP_COMMITTED", label); committed.append(final_dir); continue
            raise RuntimeError(f"existing non-success chunk: {manifest_path}")
        tmp = chunks_root/f".{label}.tmp"
        if tmp.exists():
            os.replace(tmp, chunks_root/f"{label}.abandoned_{now_cst():%Y%m%d_%H%M%S}")
        tmp.mkdir(parents=True, exist_ok=False)
        print(f"FETCH {label} {cstart} ~ {cend}")
        df, observed_at, pages = fetch_chunk(cstart,cend,tmp,a.delay,a.tries,a.max_pages)
        canonical = df.loc[df["event_class"].isin(["FORECAST","EXPRESS"])].copy()
        raw_path, can_path = tmp/"raw_query_matches.parquet", tmp/"canonical_events.parquet"
        write_parquet(df, raw_path); write_parquet(canonical, can_path)
        audit = validate(df, canonical, cstart, cend)
        if not audit["ok"]: raise RuntimeError(f"CHUNK_VALIDATION_FAILED {label}: {audit}")
        m = {
            "schema_version":"CNINFO_EARNINGS_HISTORY_BACKFILL_V01","status":"SUCCESS","chunk":label,
            "start_date":str(cstart),"end_date":str(cend),"keywords":list(KEYWORDS),
            "canonical_semantics":"EXPLICIT_FORECAST_OR_EXPRESS_TITLE_ONLY",
            "backfill_observed_at":observed_at,"hostname":socket.gethostname(),"audit":audit,
            "raw_query_matches_sha256":sha256_file(raw_path),"canonical_events_sha256":sha256_file(can_path),
            "principles":{"historical_backfill_observed_at_not_first_seen_at":True,
                          "event_time_from_cninfo_announcement_time":True,
                          "cloud_financial_values_not_used":True,
                          "production_sqlite_touched":False,"research_db_touched":False,"market_catalog_touched":False}
        }
        write_json_atomic(tmp/"chunk_manifest.json", m)
        os.replace(tmp, final_dir)
        committed.append(final_dir)
        print(f"COMMITTED {label} raw={audit['raw_dedup_rows']} canonical={audit['canonical_rows']}")

    agg = aggregate(run_dir, committed)
    summary = {
        "status":"PASS_CNINFO_HISTORICAL_EARNINGS_BACKFILL_V0_1","run_id":run_id,
        "started_at":started_at,"completed_at":iso_now(),"requested_start":str(start),"requested_end":str(end),
        "chunking":"CALENDAR_MONTH","chunks_committed":len(committed),"keywords":list(KEYWORDS),
        "canonical_semantics":"EXPLICIT_FORECAST_OR_EXPRESS_TITLE_ONLY","aggregate":agg,
        "historical_pit_semantics":{
            "event_time":"OFFICIAL_CNINFO_ANNOUNCEMENT_TIME",
            "backfill_observed_at":"POSITIONX_FETCH_TIME; NOT HISTORICAL_FIRST_SEEN",
            "trading_rule_if_researched":"Conservative next market-session OPEN; DAY precision never same-day.",
            "surprise_value_pit_ready":False,"classic_pead_ready":False
        },
        "output_dir":str(run_dir),
        "outputs":["canonical_events_all.parquet","raw_query_matches_all.parquet","monthly_summary.csv",
                   "event_class_summary.csv","backfill_summary.json"]
    }
    write_json_atomic(run_dir/"backfill_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
