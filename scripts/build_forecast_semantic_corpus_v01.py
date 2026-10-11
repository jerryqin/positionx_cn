from __future__ import annotations

r"""
PositionX — Forecast Semantic Corpus Builder V0.1

Build the full 2024-01-01 .. 2026-09-29 SH/SZ canonical FORECAST corpus
using the frozen semantic parser V0.2.2 and robust PDF downloader V0.1.1.

Architecture
------------
Input metadata:
  D:\positionx_data\events\cninfo_earnings_history_v0_1\
  runs\pilot_20240101_20260929\canonical_events_all.parquet

Output:
  D:\positionx_data\events\forecast_semantic_corpus_v0_1\
    chunks\YYYY-MM\
      pdf\<announcementId>.pdf
      semantic_events.parquet
      chunk_manifest.json
    semantic_events_all.parquet
    monthly_quality.csv
    corpus_summary.json
    manual_review_100.csv

Safety
------
- SH/SZ only; BSE excluded.
- FORECAST only.
- event_kind / ST-at-event / profit / direction semantics are delegated to the
  exact reviewed parser script and hash-checked.
- robust PDF transport is delegated to the exact reviewed downloader script.
- calendar-month chunks are immutable after SUCCESS.
- a chunk with any unexpected PDF/parser failure is NOT committed SUCCESS.
- no OCR.
- no broker/order/production SQLite/research DB/market catalog mutation.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
EVENTS = Path(
    r"D:\positionx_data\events\cninfo_earnings_history_v0_1"
    r"\runs\pilot_20240101_20260929\canonical_events_all.parquet"
)
OUT_ROOT = Path(r"D:\positionx_data\events\forecast_semantic_corpus_v0_1")

PARSER_SCRIPT = ROOT / "scripts" / "audit_forecast_semantic_parser_v02.py"
DOWNLOADER_SCRIPT = ROOT / "scripts" / "audit_forecast_pdf_text_v01.py"

EXPECTED_PARSER_SHA256 = "c31e23e39118e45b38c7f4e1b1a41e30e452aea703b38f3190f45e68febe8be4"
EXPECTED_DOWNLOADER_SHA256 = "641c3095a2a80c4a55dd31d2a70ab95c5df05ecf8245beb6559d89c3bb3fdc72"

DEFAULT_START = "2024-01-01"
DEFAULT_END = "2026-09-29"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def qpath(p: Path) -> str:
    return str(p).replace("\\", "/").replace("'", "''")


def safe(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, bool)):
        return v
    if isinstance(v, float):
        return v
    if hasattr(v, "item"):
        try:
            return safe(v.item())
        except Exception:
            pass
    return str(v)


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, default=safe),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def verify_dependencies():
    if not PARSER_SCRIPT.exists():
        raise RuntimeError(f"Parser script missing: {PARSER_SCRIPT}")
    if not DOWNLOADER_SCRIPT.exists():
        raise RuntimeError(f"Downloader script missing: {DOWNLOADER_SCRIPT}")

    parser_sha = sha256_file(PARSER_SCRIPT)
    downloader_sha = sha256_file(DOWNLOADER_SCRIPT)

    if parser_sha != EXPECTED_PARSER_SHA256:
        raise RuntimeError(
            "FAIL_CLOSED parser hash mismatch. "
            f"expected={EXPECTED_PARSER_SHA256} actual={parser_sha}"
        )
    if downloader_sha != EXPECTED_DOWNLOADER_SHA256:
        raise RuntimeError(
            "FAIL_CLOSED downloader hash mismatch. "
            f"expected={EXPECTED_DOWNLOADER_SHA256} actual={downloader_sha}"
        )

    parser = load_module("px_forecast_parser_v022", PARSER_SCRIPT)
    downloader = load_module("px_forecast_downloader_v011", DOWNLOADER_SCRIPT)
    return parser, downloader


def norm_symbol(code: Any) -> str | None:
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(digits) != 6:
        return None
    if digits.startswith(("5","6","9")):
        return digits + ".SH"
    if digits.startswith(("0","1","2","3")):
        return digits + ".SZ"
    return None


def load_events(events_path: Path, start: date, end: date) -> pd.DataFrame:
    con = duckdb.connect(":memory:")
    try:
        df = con.execute(
            f"SELECT * FROM read_parquet('{qpath(events_path)}') "
            "WHERE event_class='FORECAST'"
        ).df()
    finally:
        con.close()

    df["symbol"] = df["secCode"].map(norm_symbol)
    df["event_time_ts"] = pd.to_datetime(df["event_time"], errors="coerce")
    df["event_date"] = df["event_time_ts"].dt.date
    df = df[df["symbol"].notna()].copy()
    df = df[df["event_date"].between(start, end)].copy()
    df["event_month"] = df["event_time_ts"].dt.strftime("%Y-%m")
    df["announcementId"] = df["announcementId"].astype(str)

    if df["announcementId"].duplicated().any():
        dup = int(df["announcementId"].duplicated().sum())
        raise RuntimeError(f"FAIL_CLOSED duplicate announcementId rows={dup}")
    return df.sort_values(["event_date","announcementId"]).reset_index(drop=True)


def parse_one(r: pd.Series, pdf_path: Path, parser):
    title = str(r["announcementTitle"])
    text = parser.extract_pdf_text(pdf_path, parser.select_pdf_engine())
    section = parser.forecast_section(text)

    kind = parser.event_kind(title)
    explicit_st = bool(parser.ST_PAT.search(text[:800]))
    eligible = kind in {"ORIGINAL_FORECAST","FORECAST_REVISION"} and not explicit_st

    period = parser.report_period(title, text)
    current = parser.parse_amount_after_anchor(section)
    prior = (
        parser.parse_prior_from_table(section, current)
        or parser.parse_prior_from_prior_section(text)
    )
    yoy = parser.parse_signed_yoy(section, current)
    change_type, change_basis = parser.derive_change(section, current, prior, yoy)

    c_lo_y = parser.to_yuan(current["lo"], current["unit"]) if current else None
    c_hi_y = parser.to_yuan(current["hi"], current["unit"]) if current else None
    p_lo_y = parser.to_yuan(prior["lo"], prior["unit"]) if prior else None
    p_hi_y = parser.to_yuan(prior["hi"], prior["unit"]) if prior else None

    derived_change_pct = None
    if None not in (c_lo_y,c_hi_y,p_lo_y,p_hi_y):
        cmid = (c_lo_y+c_hi_y)/2
        pmid = (p_lo_y+p_hi_y)/2
        if abs(pmid) > 1:
            derived_change_pct = (cmid-pmid)/abs(pmid)*100

    conf = parser.confidence(eligible, period, current, change_type)

    return {
        "announcementId": str(r["announcementId"]),
        "symbol": r["symbol"],
        "secCode": r.get("secCode"),
        "secName": r.get("secName"),
        "event_time": r.get("event_time"),
        "event_precision": r.get("event_precision"),
        "event_date": str(r.get("event_date")),
        "title": title,
        "event_kind": kind,
        "explicit_st_at_event": explicit_st,
        "strategy_eligible_semantics": eligible,
        "exclusion_reason": (
            kind if kind not in {"ORIGINAL_FORECAST","FORECAST_REVISION"}
            else ("EXPLICIT_ST_AT_EVENT" if explicit_st else None)
        ),
        "report_period": period,
        "current_profit_min": current["lo"] if current else None,
        "current_profit_max": current["hi"] if current else None,
        "current_profit_unit": current["unit"] if current else None,
        "current_profit_min_yuan": c_lo_y,
        "current_profit_max_yuan": c_hi_y,
        "current_profit_evidence": current["evidence"] if current else None,
        "prior_profit_min": prior["lo"] if prior else None,
        "prior_profit_max": prior["hi"] if prior else None,
        "prior_profit_unit": prior["unit"] if prior else None,
        "prior_profit_min_yuan": p_lo_y,
        "prior_profit_max_yuan": p_hi_y,
        "prior_profit_evidence": prior["evidence"] if prior else None,
        "prior_profit_source": prior["source"] if prior else None,
        "profit_state": parser.profit_state(c_lo_y,c_hi_y),
        "reported_net_profit_yoy_min_pct": yoy["lo"] if yoy else None,
        "reported_net_profit_yoy_max_pct": yoy["hi"] if yoy else None,
        "reported_yoy_evidence": yoy["evidence"] if yoy else None,
        "profit_change_vs_prior_abs_pct": derived_change_pct,
        "change_type": change_type,
        "change_basis": change_basis,
        "parser_confidence": conf,
        "pdf_path": str(pdf_path),
        "pdf_sha256": sha256_file(pdf_path),
    }


def summarize_chunk(rdf: pd.DataFrame) -> dict:
    eligible = rdf[rdf["strategy_eligible_semantics"]].copy()
    def rate(mask):
        return float(mask.mean()) if len(mask) else None
    return {
        "rows": int(len(rdf)),
        "eligible_rows": int(len(eligible)),
        "event_kind_counts": {
            str(k): int(v) for k,v in rdf["event_kind"].value_counts().to_dict().items()
        },
        "explicit_st_at_event_count": int(rdf["explicit_st_at_event"].sum()),
        "eligible_coverage": {
            "report_period_rate": rate(eligible["report_period"].notna()),
            "current_profit_rate": rate(eligible["current_profit_min_yuan"].notna()),
            "prior_profit_rate": rate(eligible["prior_profit_min_yuan"].notna()),
            "change_type_rate": rate(eligible["change_type"]!="UNKNOWN"),
            "high_confidence_rate": rate(eligible["parser_confidence"]=="HIGH"),
        },
        "change_type_counts": {
            str(k): int(v) for k,v in eligible["change_type"].value_counts().to_dict().items()
        },
        "confidence_counts": {
            str(k): int(v) for k,v in rdf["parser_confidence"].value_counts().to_dict().items()
        },
    }


def write_parquet(df: pd.DataFrame, path: Path) -> None:
    con = duckdb.connect(":memory:")
    try:
        con.register("_df", df)
        con.execute(
            f"COPY _df TO '{qpath(path)}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        con.close()


def aggregate(out_root: Path, months: list[str]) -> dict:
    chunk_paths = [out_root/"chunks"/m/"semantic_events.parquet" for m in months]
    expr = "[" + ",".join(f"'{qpath(p)}'" for p in chunk_paths) + "]"
    all_path = out_root/"semantic_events_all.parquet"

    con = duckdb.connect(":memory:")
    try:
        con.execute(f"""
            COPY (
              SELECT * FROM read_parquet({expr})
              ORDER BY event_time, announcementId
            )
            TO '{qpath(all_path)}'
            (FORMAT PARQUET, COMPRESSION ZSTD)
        """)

        dup = int(con.execute(f"""
            SELECT count(*) - count(DISTINCT announcementId)
            FROM read_parquet('{qpath(all_path)}')
        """).fetchone()[0])
        if dup:
            raise RuntimeError(f"FAIL_CLOSED aggregate duplicate announcementIds={dup}")

        monthly = con.execute(f"""
            SELECT
              strftime(try_cast(event_time AS TIMESTAMP), '%Y-%m') AS event_month,
              count(*) AS rows,
              sum(CASE WHEN strategy_eligible_semantics THEN 1 ELSE 0 END) AS eligible_rows,
              avg(CASE WHEN strategy_eligible_semantics AND report_period IS NOT NULL THEN 1.0
                       WHEN strategy_eligible_semantics THEN 0.0 END) AS report_period_rate,
              avg(CASE WHEN strategy_eligible_semantics AND current_profit_min_yuan IS NOT NULL THEN 1.0
                       WHEN strategy_eligible_semantics THEN 0.0 END) AS current_profit_rate,
              avg(CASE WHEN strategy_eligible_semantics AND change_type<>'UNKNOWN' THEN 1.0
                       WHEN strategy_eligible_semantics THEN 0.0 END) AS change_type_rate,
              avg(CASE WHEN strategy_eligible_semantics AND parser_confidence='HIGH' THEN 1.0
                       WHEN strategy_eligible_semantics THEN 0.0 END) AS high_confidence_rate
            FROM read_parquet('{qpath(all_path)}')
            GROUP BY 1 ORDER BY 1
        """).df()
        monthly.to_csv(out_root/"monthly_quality.csv", index=False, encoding="utf-8-sig")

        stats = con.execute(f"""
            SELECT
              count(*) AS rows,
              count(DISTINCT announcementId) AS ids,
              count(DISTINCT symbol) AS symbols,
              sum(CASE WHEN strategy_eligible_semantics THEN 1 ELSE 0 END) AS eligible,
              min(event_time), max(event_time)
            FROM read_parquet('{qpath(all_path)}')
        """).fetchone()

        event_kind = dict(con.execute(f"""
            SELECT event_kind, count(*) FROM read_parquet('{qpath(all_path)}')
            GROUP BY 1 ORDER BY 1
        """).fetchall())
        change = dict(con.execute(f"""
            SELECT change_type, count(*) FROM read_parquet('{qpath(all_path)}')
            WHERE strategy_eligible_semantics
            GROUP BY 1 ORDER BY 1
        """).fetchall())
        conf = dict(con.execute(f"""
            SELECT parser_confidence, count(*) FROM read_parquet('{qpath(all_path)}')
            GROUP BY 1 ORDER BY 1
        """).fetchall())

        eligible_metrics = con.execute(f"""
            SELECT
              avg(CASE WHEN report_period IS NOT NULL THEN 1.0 ELSE 0.0 END),
              avg(CASE WHEN current_profit_min_yuan IS NOT NULL THEN 1.0 ELSE 0.0 END),
              avg(CASE WHEN prior_profit_min_yuan IS NOT NULL THEN 1.0 ELSE 0.0 END),
              avg(CASE WHEN change_type<>'UNKNOWN' THEN 1.0 ELSE 0.0 END),
              avg(CASE WHEN parser_confidence='HIGH' THEN 1.0 ELSE 0.0 END)
            FROM read_parquet('{qpath(all_path)}')
            WHERE strategy_eligible_semantics
        """).fetchone()

        # Deterministic 100-row manual-review set: exclusions, non-HIGH, then
        # representative eligible rows across change types / years.
        review = con.execute(f"""
            WITH ranked AS (
              SELECT *,
                row_number() OVER (
                  PARTITION BY coalesce(change_type,'NULL'),
                               year(try_cast(event_time AS TIMESTAMP))
                  ORDER BY announcementId
                ) AS rn
              FROM read_parquet('{qpath(all_path)}')
            )
            SELECT * EXCLUDE(rn) FROM ranked
            WHERE NOT strategy_eligible_semantics
               OR parser_confidence<>'HIGH'
               OR rn<=3
            ORDER BY
              CASE WHEN NOT strategy_eligible_semantics THEN 0
                   WHEN parser_confidence<>'HIGH' THEN 1 ELSE 2 END,
              event_time, announcementId
            LIMIT 100
        """).df()
        review.to_csv(out_root/"manual_review_100.csv", index=False, encoding="utf-8-sig")

        return {
            "rows": int(stats[0]),
            "announcement_ids": int(stats[1]),
            "symbols": int(stats[2]),
            "strategy_eligible_rows": int(stats[3]),
            "min_event_time": str(stats[4]) if stats[4] is not None else None,
            "max_event_time": str(stats[5]) if stats[5] is not None else None,
            "event_kind_counts": {str(k):int(v) for k,v in event_kind.items()},
            "change_type_counts": {str(k):int(v) for k,v in change.items()},
            "confidence_counts": {str(k):int(v) for k,v in conf.items()},
            "eligible_coverage": {
                "report_period_rate": float(eligible_metrics[0]) if eligible_metrics[0] is not None else None,
                "current_profit_rate": float(eligible_metrics[1]) if eligible_metrics[1] is not None else None,
                "prior_profit_rate": float(eligible_metrics[2]) if eligible_metrics[2] is not None else None,
                "change_type_rate": float(eligible_metrics[3]) if eligible_metrics[3] is not None else None,
                "high_confidence_rate": float(eligible_metrics[4]) if eligible_metrics[4] is not None else None,
            },
            "semantic_events_all": str(all_path),
            "semantic_events_all_sha256": sha256_file(all_path),
        }
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["check","run"])
    ap.add_argument("--events", default=str(EVENTS))
    ap.add_argument("--out-root", default=str(OUT_ROOT))
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--tries", type=int, default=6)
    ap.add_argument("--delay", type=float, default=0.35)
    args = ap.parse_args()

    parser, downloader = verify_dependencies()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if end < start:
        raise RuntimeError("end < start")

    events_path = Path(args.events)
    if not events_path.exists():
        raise RuntimeError(f"events missing: {events_path}")
    df = load_events(events_path, start, end)

    plan = (
        df.groupby("event_month")
        .agg(events=("announcementId","size"), symbols=("symbol","nunique"))
        .reset_index()
        .sort_values("event_month")
    )

    print("FORECAST_CORPUS_PLAN")
    print(f"events={len(df)} symbols={df['symbol'].nunique()} months={len(plan)}")
    print(plan.to_string(index=False))

    if args.command == "check":
        return 0

    out_root = Path(args.out_root)
    chunks_root = out_root/"chunks"
    chunks_root.mkdir(parents=True, exist_ok=True)

    months = plan["event_month"].tolist()
    for month in months:
        final_dir = chunks_root/month
        manifest_path = final_dir/"chunk_manifest.json"

        if manifest_path.exists():
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            if m.get("status") == "SUCCESS":
                print(f"SKIP_COMMITTED {month}")
                continue
            raise RuntimeError(f"Existing non-success manifest: {manifest_path}")

        tmp = chunks_root/f".{month}.tmp"
        if tmp.exists():
            # Resume an interrupted month in place. Existing valid PDFs are
            # reused by downloader.download_pdf(), which verifies the PDF file
            # before deciding whether a network request is needed.
            print(f"RESUME_PARTIAL {month} dir={tmp}")
        else:
            tmp.mkdir(parents=True, exist_ok=False)
        pdf_dir = tmp/"pdf"
        pdf_dir.mkdir(exist_ok=True)

        g = df[df["event_month"]==month].copy()
        rows = []
        errors = []

        print(f"PROCESS {month} events={len(g)}")
        for i, (_, r) in enumerate(g.iterrows(), start=1):
            ann_id = str(r["announcementId"])
            url = str(r.get("pdf_url") or "").strip()
            pdf = pdf_dir/f"{ann_id}.pdf"
            try:
                if not url:
                    raise RuntimeError("missing pdf_url")
                downloader.download_pdf(url, pdf, tries=args.tries)
                parsed = parse_one(r, pdf, parser)
                rows.append(parsed)
                time.sleep(args.delay)
                if i == 1 or i % 50 == 0 or i == len(g):
                    print(
                        f"  [{i}/{len(g)}] {ann_id} "
                        f"kind={parsed['event_kind']} "
                        f"change={parsed['change_type']} "
                        f"conf={parsed['parser_confidence']}"
                    )
            except Exception as e:
                errors.append({
                    "announcementId": ann_id,
                    "symbol": r.get("symbol"),
                    "pdf_url": url,
                    "error": f"{type(e).__name__}: {e}",
                })
                print(f"  ERROR {ann_id} {errors[-1]['error']}")

        write_json_atomic(tmp/"errors.json", errors)

        if errors:
            write_json_atomic(tmp/"chunk_manifest.json", {
                "status":"FAILED_PDF_OR_PARSE",
                "event_month":month,
                "expected_events":int(len(g)),
                "parsed_events":int(len(rows)),
                "error_count":int(len(errors)),
                "parser_sha256":EXPECTED_PARSER_SHA256,
                "downloader_sha256":EXPECTED_DOWNLOADER_SHA256,
            })
            raise RuntimeError(
                f"FAIL_CLOSED month={month} errors={len(errors)}; "
                f"evidence preserved at {tmp}"
            )

        rdf = pd.DataFrame(rows)
        if len(rdf) != len(g) or rdf["announcementId"].nunique() != len(g):
            raise RuntimeError(
                f"FAIL_CLOSED month={month} row/id mismatch "
                f"expected={len(g)} rows={len(rdf)} ids={rdf['announcementId'].nunique()}"
            )

        pq = tmp/"semantic_events.parquet"
        write_parquet(rdf, pq)
        chunk_summary = summarize_chunk(rdf)

        manifest = {
            "status":"SUCCESS",
            "schema_version":"FORECAST_SEMANTIC_CORPUS_V01",
            "event_month":month,
            "created_at":datetime.now().astimezone().isoformat(timespec="seconds"),
            "parser_sha256":EXPECTED_PARSER_SHA256,
            "downloader_sha256":EXPECTED_DOWNLOADER_SHA256,
            "semantic_events_sha256":sha256_file(pq),
            "summary":chunk_summary,
            "principles":{
                "canonical_forecast_metadata_source":True,
                "sh_sz_only":True,
                "bse_excluded":True,
                "explicit_st_at_event_excluded_from_strategy":True,
                "subsidiary_followup_excluded_from_strategy":True,
                "pdf_raw_preserved":True,
                "no_ocr":True,
                "no_database_mutation":True,
            },
        }
        write_json_atomic(tmp/"chunk_manifest.json", manifest)
        os.replace(tmp, final_dir)
        print(
            f"COMMITTED {month} rows={chunk_summary['rows']} "
            f"eligible={chunk_summary['eligible_rows']} "
            f"high={chunk_summary['eligible_coverage']['high_confidence_rate']}"
        )

    aggregate_summary = aggregate(out_root, months)
    summary = {
        "status":"PASS_FORECAST_SEMANTIC_CORPUS_V0_1",
        "generated_at":datetime.now().astimezone().isoformat(timespec="seconds"),
        "requested_start":str(start),
        "requested_end":str(end),
        "parser_sha256":EXPECTED_PARSER_SHA256,
        "downloader_sha256":EXPECTED_DOWNLOADER_SHA256,
        "months_committed":len(months),
        "aggregate":aggregate_summary,
        "strategy_readiness_gate":{
            "automatic":(
                aggregate_summary["eligible_coverage"]["current_profit_rate"] >= 0.90
                and aggregate_summary["eligible_coverage"]["change_type_rate"] >= 0.80
                and aggregate_summary["eligible_coverage"]["high_confidence_rate"] >= 0.75
            ),
            "manual_review_required":True,
            "next_if_pass":"EARNINGS_DRIFT_STRATEGY_AUDIT_V0_1",
        },
        "no_ocr":True,
        "no_database_mutation":True,
        "output_dir":str(out_root),
        "outputs":[
            "semantic_events_all.parquet",
            "monthly_quality.csv",
            "manual_review_100.csv",
            "corpus_summary.json",
        ],
    }
    write_json_atomic(out_root/"corpus_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
