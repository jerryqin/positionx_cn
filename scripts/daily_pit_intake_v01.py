from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

try:
    import duckdb
except ImportError as e:
    raise RuntimeError("duckdb is required for Daily PIT Intake V0.1") from e


ROOT = Path(__file__).resolve().parents[1]
PIT_ROOT = Path(r"D:\positionx_data\pit_live")
RAW_ROOT = PIT_ROOT / "raw"
MANIFEST_ROOT = PIT_ROOT / "manifests"
LOG_ROOT = PIT_ROOT / "logs"
RESEARCH_DB = ROOT / "data" / "research" / "positionx_research.duckdb"

CST = timezone(timedelta(hours=8))

INDEX_SECTORS = {
    "000300.SH": ("沪深300", 300),
    "000905.SH": ("中证500", 500),
    "000852.SH": ("中证1000", 1000),
}

CNINFO_API = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC = "http://static.cninfo.com.cn/"
CNINFO_HDRS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
    "X-Requested-With": "XMLHttpRequest",
}
EARNINGS_PAT = re.compile(
    r"业绩预告|业绩快报|业绩预增|业绩预减|预盈|预亏|"
    r"首亏|续亏|扭亏|年度业绩|半年度业绩|季度业绩|"
    r"主要经营数据|经营业绩|业绩预披露"
)


# CNINFO search runs with tabName=fulltext, so a query-keyword match is NOT
# automatically an earnings-event title. Raw query matches remain immutable
# evidence; only title-confirmed rows enter the canonical earnings-event table.
FORECAST_TITLE_PAT = re.compile(
    r"业绩预告|业绩预增|业绩预减|预盈|预亏|首亏|续亏|扭亏"
)
EXPRESS_TITLE_PAT = re.compile(r"业绩快报")


def classify_earnings_event_title(title: Any) -> str | None:
    """
    Canonical Strategy-event classifier.

    Keep only explicit earnings forecast / earnings express titles.
    Broad phrases such as 年度业绩 / 半年度业绩 / 季度业绩 are NOT sufficient
    because they also match non-event notices such as 业绩说明会预告.
    """
    t = str(title or "")
    if EXPRESS_TITLE_PAT.search(t):
        return "EXPRESS"
    if FORECAST_TITLE_PAT.search(t):
        return "FORECAST"
    return None

# The revisit window is intentionally much longer than "new-event" cadence.
# There is no reliable update_time upstream. Re-reading recent business history
# is how we detect late backfills and in-place overwrites.
CLOUD_DATASETS = {
    "forecast": {
        "table": "iwaos_forecast",
        "date_field": "ann_date",
        "revisit_days": 180,
        "natural_key": ["stock_code", "ann_date", "end_date"],
        "columns": [
            "stock_code", "ann_date", "first_ann_date", "end_date", "type",
            "p_change_min", "p_change_max", "net_profit_min", "net_profit_max",
            "last_parent_net", "update_flag",
        ],
        "source_role": "HISTORICAL/BACKFILL ONLY; cninfo is forward event-time source",
    },
    "express": {
        "table": "iwaos_express",
        "date_field": "ann_date",
        "revisit_days": 180,
        "natural_key": ["stock_code", "ann_date", "end_date"],
        "columns": [
            "stock_code", "ann_date", "end_date", "n_income",
            "diluted_eps", "yoy_net_profit", "update_flag",
        ],
        "source_role": "BACKFILL/VALUE SNAPSHOT",
    },
    "income": {
        "table": "iwaos_income",
        "date_field": "ann_date",
        "revisit_days": 180,
        "natural_key": ["stock_code", "end_date", "report_type", "ann_date"],
        "columns": [
            "stock_code", "ann_date", "f_ann_date", "end_date", "report_type",
            "n_income_attr_p", "basic_eps", "total_revenue", "update_flag",
        ],
        "source_role": "BACKFILL/VALUE SNAPSHOT",
    },
    "fina_indicator": {
        "table": "iwaos_fina_indicator",
        "date_field": "ann_date",
        "revisit_days": 180,
        "natural_key": ["stock_code", "end_date", "ann_date"],
        "columns": [
            "stock_code", "ann_date", "end_date", "eps", "dt_eps",
            "netprofit_yoy", "basic_eps_yoy", "profit_dedt",
        ],
        "source_role": "BACKFILL/VALUE SNAPSHOT",
    },
    "suspend_d": {
        "table": "iwaos_suspend_d",
        "date_field": "trade_date",
        "revisit_days": 30,
        "natural_key": ["stock_code", "trade_date", "suspend_type"],
        "columns": None,
        "source_role": "FORWARD SUPPORT WHEN FRESH",
    },
    "stock_limit": {
        "table": "iwaos_stock_limit",
        "date_field": "trade_date",
        "revisit_days": 30,
        "natural_key": ["stock_code", "trade_date"],
        "columns": ["stock_code", "trade_date", "up_limit", "down_limit"],
        "source_role": "OBSERVE ONLY UNTIL FRESHNESS RECOVERS",
    },
    "stock_basic": {
        "table": "iwaos_stock_basic",
        "date_field": None,
        "revisit_days": None,
        "natural_key": ["stock_code", "act_name"],
        "columns": None,
        "source_role": "DAILY FULL SNAPSHOT",
    },
}

INDEX_WEIGHT_CODES = ("000300.SH", "000905.SH", "000852.SH")


def now_cst() -> datetime:
    return datetime.now(CST)


def iso_now() -> str:
    return now_cst().isoformat(timespec="seconds")


def safe_capture_id(phase: str) -> str:
    return now_cst().strftime("%Y%m%dT%H%M%S+0800") + "_" + phase


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def payload_hash_row(row: dict[str, Any]) -> str:
    s = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def write_parquet(df: pd.DataFrame, path: Path) -> None:
    """Write parquet through DuckDB so PositionX does not require pyarrow."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(database=":memory:")
    try:
        con.register("_df", df)
        qpath = str(path).replace("'", "''")
        con.execute(f"COPY _df TO '{qpath}' (FORMAT PARQUET)")
    finally:
        con.close()


def init_catalog(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS pit_capture_runs (
            capture_id VARCHAR,
            phase VARCHAR,
            observed_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            status VARCHAR,
            raw_dir VARCHAR,
            manifest_path VARCHAR,
            hostname VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS pit_capture_items (
            capture_id VARCHAR,
            dataset VARCHAR,
            source VARCHAR,
            rows BIGINT,
            source_max_date VARCHAR,
            health VARCHAR,
            file_path VARCHAR,
            sha256 VARCHAR,
            error VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS pit_index_membership_observed (
            capture_id VARCHAR,
            snapshot_date VARCHAR,
            observed_at TIMESTAMPTZ,
            index_code VARCHAR,
            index_name VARCHAR,
            symbol VARCHAR,
            source VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS pit_cninfo_announcement_versions (
            announcement_id VARCHAR,
            payload_hash VARCHAR,
            first_seen_at TIMESTAMPTZ,
            last_seen_at TIMESTAMPTZ,
            event_time VARCHAR,
            event_precision VARCHAR,
            sec_code VARCHAR,
            sec_name VARCHAR,
            title VARCHAR,
            pdf_url VARCHAR,
            raw_file_path VARCHAR
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS pit_cninfo_earnings_event_versions (
            announcement_id VARCHAR,
            payload_hash VARCHAR,
            first_seen_at TIMESTAMPTZ,
            last_seen_at TIMESTAMPTZ,
            event_time VARCHAR,
            event_precision VARCHAR,
            sec_code VARCHAR,
            sec_name VARCHAR,
            title VARCHAR,
            event_class VARCHAR,
            query_keyword VARCHAR,
            pdf_url VARCHAR,
            raw_file_path VARCHAR
        )
    """)


def insert_capture_run(
    con: duckdb.DuckDBPyConnection,
    capture_id: str,
    phase: str,
    observed_at: str,
    finished_at: str,
    status: str,
    raw_dir: Path,
    manifest_path: Path,
) -> None:
    con.execute(
        "DELETE FROM pit_capture_runs WHERE capture_id = ?",
        [capture_id],
    )
    con.execute(
        """
        INSERT INTO pit_capture_runs
        VALUES (?, ?, CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ),
                ?, ?, ?, ?)
        """,
        [
            capture_id, phase, observed_at, finished_at, status,
            str(raw_dir), str(manifest_path), socket.gethostname(),
        ],
    )


def insert_capture_item(
    con: duckdb.DuckDBPyConnection,
    capture_id: str,
    item: dict[str, Any],
) -> None:
    con.execute(
        """
        INSERT INTO pit_capture_items
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            capture_id,
            item.get("dataset"),
            item.get("source"),
            int(item.get("rows", 0)),
            item.get("source_max_date"),
            item.get("health"),
            item.get("file_path"),
            item.get("sha256"),
            item.get("error"),
        ],
    )


def normalize_symbol(s: Any) -> str:
    x = str(s).strip().upper()
    if "." in x:
        return x
    digits = "".join(ch for ch in x if ch.isdigit())
    if len(digits) != 6:
        return x
    if digits.startswith(("5", "6", "9")):
        return digits + ".SH"
    if digits.startswith(("0", "1", "2", "3")):
        return digits + ".SZ"
    if digits.startswith(("4", "8")):
        return digits + ".BJ"
    return x


def capture_qmt_membership(
    capture_id: str,
    observed_at: str,
    dest: Path,
) -> tuple[list[dict[str, Any]], bool]:
    try:
        from xtquant import xtdata
    except Exception as e:
        return [{
            "dataset": "index_membership",
            "source": "QMT_CURRENT",
            "rows": 0,
            "source_max_date": None,
            "health": "ERROR",
            "file_path": None,
            "sha256": None,
            "error": f"xtquant import failed: {e}",
        }], False

    snapshot_date = now_cst().strftime("%Y%m%d")
    rows = []
    ok = True
    for index_code, (sector, expected_count) in INDEX_SECTORS.items():
        try:
            members = xtdata.get_stock_list_in_sector(sector)
            symbols = sorted({normalize_symbol(x) for x in (members or [])})
            health = "FRESH" if len(symbols) == expected_count else "INVALID_COUNT"
            if health != "FRESH":
                ok = False
            for symbol in symbols:
                rows.append({
                    "capture_id": capture_id,
                    "snapshot_date": snapshot_date,
                    "observed_at": observed_at,
                    "index_code": index_code,
                    "index_name": sector,
                    "symbol": symbol,
                    "source": "QMT_CURRENT",
                })
        except Exception as e:
            ok = False
            rows.append({
                "capture_id": capture_id,
                "snapshot_date": snapshot_date,
                "observed_at": observed_at,
                "index_code": index_code,
                "index_name": sector,
                "symbol": f"__ERROR__:{e}",
                "source": "QMT_CURRENT",
            })

    df = pd.DataFrame(rows)
    path = dest / "index_membership_qmt.parquet"
    write_parquet(df, path)

    items = []
    for index_code, (sector, expected_count) in INDEX_SECTORS.items():
        g = df[
            (df["index_code"] == index_code)
            & (~df["symbol"].astype(str).str.startswith("__ERROR__"))
        ]
        health = "FRESH" if len(g) == expected_count else "INVALID_COUNT"
        items.append({
            "dataset": f"index_membership_{index_code}",
            "source": "QMT_CURRENT",
            "rows": int(len(g)),
            "source_max_date": snapshot_date,
            "health": health,
            "file_path": str(path),
            "sha256": sha256_file(path),
            "error": None if health == "FRESH"
                     else f"expected={expected_count}, actual={len(g)}",
        })
    return items, ok


def cninfo_post(
    se_date: str,
    page_num: int,
    searchkey: str,
    tries: int = 4,
) -> dict:
    body = urllib.parse.urlencode({
        "pageNum": page_num,
        "pageSize": 30,
        "column": "",
        "tabName": "fulltext",
        "plate": "",
        "stock": "",
        "searchkey": searchkey,
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": se_date,
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }).encode()
    req = urllib.request.Request(
        CNINFO_API, data=body, method="POST", headers=CNINFO_HDRS
    )
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                chunks = []
                while True:
                    b = r.read(65536)
                    if not b:
                        break
                    chunks.append(b)
            return json.loads(b"".join(chunks).decode("utf-8", "replace"))
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
    raise RuntimeError(
        f"cninfo failed date={se_date} page={page_num}: {last!r}"
    )


def fetch_cninfo_window(days: int = 3, delay: float = 0.6) -> pd.DataFrame:
    end = now_cst().date()
    start = end - timedelta(days=days)
    rows = []
    for delta in range((end - start).days + 1):
        d = start + timedelta(days=delta)
        for kw in ("业绩预告", "业绩快报"):
            first = cninfo_post(str(d), 1, kw)
            total_pages = int(first.get("totalpages") or 0)
            pages = [first]
            for p in range(2, total_pages + 1):
                time.sleep(delay)
                pages.append(cninfo_post(str(d), p, kw))
            for payload in pages:
                for row in payload.get("announcements") or []:
                    row = dict(row)
                    row["query_keyword"] = kw
                    rows.append(row)
            time.sleep(delay)

    if not rows:
        return pd.DataFrame(columns=[
            "announcementId", "secCode", "secName", "announcementTitle",
            "announcementTime", "storageTime", "adjunctUrl",
            "query_keyword", "event_time_iso", "event_precision",
            "pdf_url", "event_class", "is_earnings_event",
        ])

    df = pd.DataFrame(rows)
    for c in (
        "announcementId", "secCode", "secName", "announcementTitle",
        "announcementTime", "storageTime", "adjunctUrl", "query_keyword",
    ):
        if c not in df.columns:
            df[c] = None

    df["announcementId"] = df["announcementId"].astype(str)
    df["announcementTitle"] = (
        df["announcementTitle"]
        .fillna("")
        .astype(str)
        .str.replace(r"</?em>", "", regex=True)
    )
    df = df.drop_duplicates(subset=["announcementId"], keep="first")

    et = pd.to_numeric(df["announcementTime"], errors="coerce")
    local = pd.to_datetime(et, unit="ms", utc=True, errors="coerce").dt.tz_convert(CST)
    df["event_time_iso"] = local.dt.strftime("%Y-%m-%d %H:%M:%S")
    df["event_precision"] = local.dt.strftime("%H:%M:%S").map(
        lambda x: "DAY" if x == "00:00:00" else "SECOND"
    )
    df["pdf_url"] = CNINFO_STATIC + df["adjunctUrl"].fillna("").astype(str)
    df["event_class"] = df["announcementTitle"].map(classify_earnings_event_title)
    df["is_earnings_event"] = df["event_class"].notna()
    return df


def update_cninfo_catalog(
    con: duckdb.DuckDBPyConnection,
    df: pd.DataFrame,
    observed_at: str,
    raw_path: Path,
) -> None:
    if df.empty:
        return

    existing = con.execute(
        """
        SELECT announcement_id, payload_hash, CAST(first_seen_at AS VARCHAR)
        FROM pit_cninfo_announcement_versions
        """
    ).fetchall()
    first_seen_by_id = {}
    existing_version = set()
    for ann_id, ph, first_seen in existing:
        first_seen_by_id.setdefault(str(ann_id), first_seen)
        existing_version.add((str(ann_id), str(ph)))

    for rec in df.to_dict(orient="records"):
        canonical = {
            "announcementId": rec.get("announcementId"),
            "secCode": rec.get("secCode"),
            "secName": rec.get("secName"),
            "announcementTitle": rec.get("announcementTitle"),
            "announcementTime": rec.get("announcementTime"),
            "storageTime": rec.get("storageTime"),
            "adjunctUrl": rec.get("adjunctUrl"),
        }
        ann_id = str(rec.get("announcementId"))
        ph = payload_hash_row(canonical)
        if (ann_id, ph) in existing_version:
            con.execute(
                """
                UPDATE pit_cninfo_announcement_versions
                SET last_seen_at = CAST(? AS TIMESTAMPTZ)
                WHERE announcement_id=? AND payload_hash=?
                """,
                [observed_at, ann_id, ph],
            )
            continue

        first_seen = first_seen_by_id.get(ann_id, observed_at)
        con.execute(
            """
            INSERT INTO pit_cninfo_announcement_versions
            VALUES (?, ?, CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ),
                    ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ann_id, ph, first_seen, observed_at,
                rec.get("event_time_iso"), rec.get("event_precision"),
                str(rec.get("secCode") or ""),
                str(rec.get("secName") or ""),
                str(rec.get("announcementTitle") or ""),
                str(rec.get("pdf_url") or ""),
                str(raw_path),
            ],
        )



def update_cninfo_earnings_event_catalog(
    con: duckdb.DuckDBPyConnection,
    df: pd.DataFrame,
    observed_at: str,
    raw_path: Path,
) -> None:
    """
    Canonical earnings-event metadata.

    CNINFO query uses tabName=fulltext. Therefore only title-confirmed earnings
    events are allowed here. pit_cninfo_announcement_versions remains the raw
    full-text-query-match version catalog for provenance/backward compatibility.
    """
    if df.empty:
        return

    bad = df.loc[~df["is_earnings_event"].fillna(False)]
    if not bad.empty:
        raise RuntimeError(
            "Canonical CNINFO earnings catalog received non-title-confirmed rows."
        )

    existing = con.execute(
        """
        SELECT announcement_id, payload_hash, CAST(first_seen_at AS VARCHAR)
        FROM pit_cninfo_earnings_event_versions
        """
    ).fetchall()
    first_seen_by_id = {}
    existing_version = set()
    for ann_id, ph, first_seen in existing:
        first_seen_by_id.setdefault(str(ann_id), first_seen)
        existing_version.add((str(ann_id), str(ph)))

    for rec in df.to_dict(orient="records"):
        canonical = {
            "announcementId": rec.get("announcementId"),
            "secCode": rec.get("secCode"),
            "secName": rec.get("secName"),
            "announcementTitle": rec.get("announcementTitle"),
            "announcementTime": rec.get("announcementTime"),
            "storageTime": rec.get("storageTime"),
            "adjunctUrl": rec.get("adjunctUrl"),
        }
        ann_id = str(rec.get("announcementId"))
        ph = payload_hash_row(canonical)

        if (ann_id, ph) in existing_version:
            con.execute(
                """
                UPDATE pit_cninfo_earnings_event_versions
                SET last_seen_at = CAST(? AS TIMESTAMPTZ)
                WHERE announcement_id=? AND payload_hash=?
                """,
                [observed_at, ann_id, ph],
            )
            continue

        first_seen = first_seen_by_id.get(ann_id, observed_at)
        con.execute(
            """
            INSERT INTO pit_cninfo_earnings_event_versions
            VALUES (?, ?, CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ),
                    ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ann_id,
                ph,
                first_seen,
                observed_at,
                rec.get("event_time_iso"),
                rec.get("event_precision"),
                str(rec.get("secCode") or ""),
                str(rec.get("secName") or ""),
                str(rec.get("announcementTitle") or ""),
                str(rec.get("event_class") or ""),
                str(rec.get("query_keyword") or ""),
                str(rec.get("pdf_url") or ""),
                str(raw_path),
            ],
        )

def cloud_query(sql: str, tries: int = 4) -> pd.DataFrame:
    from iwa_data import cloud_query as _query
    last = None
    for i in range(tries):
        try:
            return _query(sql)
        except Exception as e:
            last = e
            time.sleep(2.0 * (i + 1))
    raise RuntimeError(f"iwa cloud query failed: {last!r}")


def cloud_sql(cfg: dict[str, Any], start: str, end: str) -> str:
    cols = "*" if not cfg["columns"] else ", ".join(cfg["columns"])
    table = cfg["table"]
    field = cfg["date_field"]
    if field is None:
        return f"SELECT {cols} FROM {table} LIMIT 100000"
    return (
        f"SELECT {cols} FROM {table} "
        f"WHERE {field}>='{start}' AND {field}<='{end}' LIMIT 500000"
    )


def max_date_string(df: pd.DataFrame, field: str | None) -> str | None:
    if field is None or field not in df.columns or df.empty:
        return None
    x = pd.to_datetime(df[field], errors="coerce")
    if x.notna().sum() == 0:
        return None
    return x.max().strftime("%Y-%m-%d")


def cloud_health(name: str, source_max: str | None) -> str:
    if source_max is None:
        return "UNKNOWN"
    latest = datetime.strptime(source_max, "%Y-%m-%d").date()
    lag = (now_cst().date() - latest).days

    # Only stock_limit has a truly daily expected business cadence here.
    # 5 calendar days tolerates weekends; longer holidays may yield UNKNOWN
    # rather than causing a false canonical PASS.
    if name == "stock_limit":
        return "FRESH" if lag <= 5 else "STALE"

    if name in ("forecast", "express", "income", "fina_indicator"):
        # Event-driven tables cannot be declared fresh from max(event_date)
        # alone. Preserve the snapshot, expose the source date, and never
        # promote it to forward event-time truth.
        return "UNKNOWN"

    if name == "suspend_d":
        return "UNKNOWN"

    if name == "stock_basic":
        return "SNAPSHOT"

    return "UNKNOWN"


def capture_cloud(dest: Path) -> tuple[list[dict[str, Any]], bool]:
    items = []
    overall_ok = True
    today = now_cst().date()

    # Curated narrow-column queries avoid 108-column timeouts while preserving
    # the fields PositionX currently considers economically useful.
    for name, cfg in CLOUD_DATASETS.items():
        revisit = cfg["revisit_days"]
        start = (
            (today - timedelta(days=revisit)).strftime("%Y-%m-%d")
            if revisit is not None else None
        )
        end = today.strftime("%Y-%m-%d")
        try:
            sql = cloud_sql(cfg, start, end)
            df = cloud_query(sql)
            if df is None:
                df = pd.DataFrame()
            if not df.empty:
                df = df.drop_duplicates(ignore_index=True)
            path = dest / f"iwa_{name}.parquet"
            write_parquet(df, path)
            source_max = max_date_string(df, cfg["date_field"])
            items.append({
                "dataset": name,
                "source": "IWAOS_CLOUD",
                "rows": int(len(df)),
                "source_max_date": source_max,
                "health": cloud_health(name, source_max),
                "file_path": str(path),
                "sha256": sha256_file(path),
                "error": None,
                "revisit_days": revisit,
                "natural_key": cfg["natural_key"],
                "source_role": cfg["source_role"],
            })
        except Exception as e:
            overall_ok = False
            items.append({
                "dataset": name,
                "source": "IWAOS_CLOUD",
                "rows": 0,
                "source_max_date": None,
                "health": "ERROR",
                "file_path": None,
                "sha256": None,
                "error": str(e)[:500],
                "revisit_days": revisit,
                "natural_key": cfg["natural_key"],
                "source_role": cfg["source_role"],
            })

    # index_weight must be queried one code at a time.
    parts = []
    errors = []
    start = (today - timedelta(days=120)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    for code in INDEX_WEIGHT_CODES:
        try:
            df = cloud_query(
                "SELECT index_code,trade_date,concept_code,weight "
                "FROM iwaos_index_weight "
                f"WHERE index_code='{code}' "
                f"AND trade_date>='{start}' AND trade_date<='{end}' "
                "LIMIT 100000"
            )
            if df is not None and not df.empty:
                parts.append(df.drop_duplicates(ignore_index=True))
        except Exception as e:
            errors.append(f"{code}: {e}")

    idx = (
        pd.concat(parts, ignore_index=True).drop_duplicates(ignore_index=True)
        if parts else pd.DataFrame(
            columns=["index_code", "trade_date", "concept_code", "weight"]
        )
    )
    ipath = dest / "iwa_index_weight.parquet"
    write_parquet(idx, ipath)
    items.append({
        "dataset": "index_weight",
        "source": "IWAOS_CLOUD",
        "rows": int(len(idx)),
        "source_max_date": max_date_string(idx, "trade_date"),
        "health": "PERIODIC_SNAPSHOT",
        "file_path": str(ipath),
        "sha256": sha256_file(ipath),
        "error": "; ".join(errors) if errors else None,
        "revisit_days": 120,
        "natural_key": ["index_code", "trade_date", "concept_code"],
        "source_role": "WEIGHT/AUDIT ONLY; NOT DAILY MEMBERSHIP TRUTH",
    })
    if errors:
        overall_ok = False

    return items, overall_ok


def run(phase: str) -> int:
    observed_at = iso_now()
    capture_id = safe_capture_id(phase)
    date_dir = now_cst().strftime("%Y%m%d")
    dest = RAW_ROOT / date_dir / phase / capture_id
    dest.mkdir(parents=True, exist_ok=False)
    MANIFEST_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    manifest_path = MANIFEST_ROOT / f"{capture_id}.json"

    items: list[dict[str, Any]] = []
    required_ok = True

    if phase == "preopen":
        qitems, qok = capture_qmt_membership(capture_id, observed_at, dest)
        items.extend(qitems)
        required_ok = required_ok and qok

    # cninfo is required in both phases: this is the forward event-time source.
    try:
        cdf = fetch_cninfo_window(days=3)

        # Immutable raw full-text query matches.
        cpath = dest / "cninfo_earnings_announcements.parquet"
        write_parquet(cdf, cpath)

        # Canonical Strategy-event metadata: explicit FORECAST / EXPRESS titles only.
        edf = cdf.loc[
            cdf["event_class"].isin(["FORECAST", "EXPRESS"])
        ].copy()
        epath = dest / "cninfo_earnings_title_confirmed.parquet"
        write_parquet(edf, epath)

        source_max = (
            max([str(x)[:10] for x in cdf["event_time_iso"].dropna()])
            if len(cdf) else None
        )
        event_source_max = (
            max([str(x)[:10] for x in edf["event_time_iso"].dropna()])
            if len(edf) else None
        )

        citem = {
            "dataset": "cninfo_earnings_announcements",
            "source": "CNINFO_PUBLIC",
            "rows": int(len(cdf)),
            "source_max_date": source_max,
            "health": "FRESH_QUERY_OK",
            "file_path": str(cpath),
            "sha256": sha256_file(cpath),
            "error": None,
            "source_role": "RAW_FULLTEXT_QUERY_MATCHES; NOT CANONICAL EARNINGS EVENTS",
            "title_confirmed_rows": int(len(edf)),
        }
        items.append(citem)
        items.append({
            "dataset": "cninfo_earnings_title_confirmed",
            "source": "CNINFO_PUBLIC",
            "rows": int(len(edf)),
            "source_max_date": event_source_max,
            "health": "FRESH_QUERY_OK",
            "file_path": str(epath),
            "sha256": sha256_file(epath),
            "error": None,
            "source_role": "CANONICAL_FORWARD_EARNINGS_STRATEGY_EVENT_METADATA",
        })
    except Exception as e:
        cdf = pd.DataFrame()
        edf = pd.DataFrame()
        cpath = None
        epath = None
        items.append({
            "dataset": "cninfo_earnings_announcements",
            "source": "CNINFO_PUBLIC",
            "rows": 0,
            "source_max_date": None,
            "health": "ERROR",
            "file_path": None,
            "sha256": None,
            "error": str(e)[:500],
            "source_role": "RAW_FULLTEXT_QUERY_MATCHES; NOT CANONICAL EARNINGS EVENTS",
        })
        items.append({
            "dataset": "cninfo_earnings_title_confirmed",
            "source": "CNINFO_PUBLIC",
            "rows": 0,
            "source_max_date": None,
            "health": "ERROR",
            "file_path": None,
            "sha256": None,
            "error": str(e)[:500],
            "source_role": "CANONICAL_FORWARD_EARNINGS_STRATEGY_EVENT_METADATA",
        })
        required_ok = False

    # Cloud source is post-close only. It is evidence/backfill, not trusted
    # forward event-time truth for currently stale datasets.
    if phase == "postclose":
        cloud_items, cloud_ok = capture_cloud(dest)
        items.extend(cloud_items)
        # Cloud failures are operationally important but do not invalidate
        # cninfo/QMT raw evidence. We mark PARTIAL and return nonzero so Task
        # Scheduler can retry.
        required_ok = required_ok and cloud_ok

    finished_at = iso_now()
    status = "SUCCESS" if required_ok else "PARTIAL"

    manifest = {
        "schema_version": "DAILY_PIT_V01",
        "capture_id": capture_id,
        "phase": phase,
        "observed_at": observed_at,
        "finished_at": finished_at,
        "status": status,
        "raw_dir": str(dest),
        "research_db": str(RESEARCH_DB),
        "principles": {
            "raw_snapshots_immutable": True,
            "same_day_runs_never_overwrite": True,
            "event_time_and_observed_at_separate": True,
            "qmt_current_membership_is_forward_truth": True,
            "cninfo_is_forward_earnings_event_time_source": True,
            "cninfo_fulltext_query_matches_are_raw_evidence": True,
            "cninfo_title_confirmed_events_are_canonical": True,
            "cninfo_canonical_event_classes": ["FORECAST", "EXPRESS"],
            "cninfo_broad_earnings_phrases_not_sufficient_for_canonical": True,
            "cninfo_semantics_version": "V0.3",
            "cloud_financial_tables_are_backfill_snapshot_sources": True,
            "production_sqlite_touched": False,
        },
        "items": items,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # Catalog is thin and reconstructable. Raw parquet remains source of truth.
    RESEARCH_DB.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(RESEARCH_DB))
    try:
        init_catalog(con)
        con.execute("BEGIN TRANSACTION")
        for item in items:
            insert_capture_item(con, capture_id, item)

        if phase == "preopen":
            mp = dest / "index_membership_qmt.parquet"
            if mp.exists():
                qpath = str(mp).replace("'", "''")
                con.execute(
                    f"""
                    INSERT INTO pit_index_membership_observed
                    SELECT
                        capture_id,
                        snapshot_date,
                        CAST(observed_at AS TIMESTAMPTZ),
                        index_code,
                        index_name,
                        symbol,
                        source
                    FROM read_parquet('{qpath}')
                    WHERE NOT starts_with(symbol, '__ERROR__')
                    """
                )

        if cpath is not None and cpath.exists():
            # Backward-compatible raw full-text-query-match version catalog.
            update_cninfo_catalog(
                con,
                cdf,
                observed_at=observed_at,
                raw_path=cpath,
            )

            # Canonical title-confirmed earnings-event metadata.
            update_cninfo_earnings_event_catalog(
                con,
                edf,
                observed_at=observed_at,
                raw_path=cpath,
            )

        insert_capture_run(
            con, capture_id, phase, observed_at, finished_at,
            status, dest, manifest_path,
        )
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()

    print("=" * 110)
    print("POSITIONX DAILY PIT INTAKE V0.1")
    print("=" * 110)
    print("capture_id:", capture_id)
    print("phase:", phase)
    print("status:", status)
    print("raw:", dest)
    print("manifest:", manifest_path)
    print()
    for item in items:
        print(
            f"{item['dataset']:<38} "
            f"rows={item['rows']:<8} "
            f"health={item['health']:<20} "
            f"max={item.get('source_max_date')}"
        )
        if item.get("error"):
            print("  ERROR:", item["error"])
    print()
    print("Raw snapshots are immutable.")
    print("Production SQLite was not opened.")
    print("No broker / order / execution action was performed.")
    return 0 if status == "SUCCESS" else 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--phase",
        choices=("preopen", "postclose"),
        required=True,
    )
    a = ap.parse_args()
    return run(a.phase)


if __name__ == "__main__":
    sys.exit(main())
