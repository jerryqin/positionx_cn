from __future__ import annotations

r"""
PositionX — Forecast Semantic Corpus Local Reparse V0.2.3

Purpose
-------
Reparse the completed 2024-01-01 .. 2026-09-29 SH/SZ FORECAST corpus using the
frozen Semantic Parser V0.2.3, reusing the 12,090 PDFs already preserved by
Forecast Semantic Corpus V0.1.

This script performs NO network requests and does NOT overwrite the V0.1 corpus.

Source corpus:
  D:\positionx_data\events\forecast_semantic_corpus_v0_1\

Output corpus:
  D:\positionx_data\events\forecast_semantic_corpus_v0_2_3\

Safety
------
- exact V0.2.3 parser hash required;
- exact reviewed V0.1.1 builder hash required as compatibility evidence;
- source V0.1 corpus must report PASS;
- source PDFs are read-only;
- no downloader import/call;
- no OCR;
- no broker/order/production SQLite/research DB/market catalog mutation;
- each output month is atomically committed;
- old corpus remains untouched;
- writes a semantic diff against V0.1 after aggregate completion.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
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

SOURCE_CORPUS_ROOT = Path(
    r"D:\positionx_data\events\forecast_semantic_corpus_v0_1"
)
OUT_ROOT = Path(
    r"D:\positionx_data\events\forecast_semantic_corpus_v0_2_3"
)

PARSER_SCRIPT = ROOT / "scripts" / "audit_forecast_semantic_parser_v02.py"
BASE_BUILDER_SCRIPT = ROOT / "scripts" / "build_forecast_semantic_corpus_v01.py"

EXPECTED_PARSER_SHA256 = "7cf5dcd39d584a13a1095b482beb8b2298b26b381f900f13f18c0f8cd28e7ed4"
EXPECTED_BASE_BUILDER_SHA256 = "64224dd91203cd9906ab1c04bc6554064d0b422147e31eac4038da8b02188f68"

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


def verify_dependencies(source_root: Path):
    if not PARSER_SCRIPT.exists():
        raise RuntimeError(f"Parser script missing: {PARSER_SCRIPT}")
    if not BASE_BUILDER_SCRIPT.exists():
        raise RuntimeError(f"Base builder missing: {BASE_BUILDER_SCRIPT}")

    parser_sha = sha256_file(PARSER_SCRIPT)
    builder_sha = sha256_file(BASE_BUILDER_SCRIPT)

    if parser_sha != EXPECTED_PARSER_SHA256:
        raise RuntimeError(
            "FAIL_CLOSED parser hash mismatch. "
            f"expected={EXPECTED_PARSER_SHA256} actual={parser_sha}"
        )
    if builder_sha != EXPECTED_BASE_BUILDER_SHA256:
        raise RuntimeError(
            "FAIL_CLOSED base builder hash mismatch. "
            f"expected={EXPECTED_BASE_BUILDER_SHA256} actual={builder_sha}"
        )

    summary_path = source_root / "corpus_summary.json"
    if not summary_path.exists():
        raise RuntimeError(f"Source corpus summary missing: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("status") != "PASS_FORECAST_SEMANTIC_CORPUS_V0_1":
        raise RuntimeError(
            f"FAIL_CLOSED source corpus is not PASS: {summary.get('status')}"
        )
    if int(summary.get("months_committed") or 0) != 30:
        raise RuntimeError(
            f"FAIL_CLOSED source months_committed={summary.get('months_committed')}"
        )
    agg = summary.get("aggregate") or {}
    if int(agg.get("rows") or 0) != 12090:
        raise RuntimeError(
            f"FAIL_CLOSED source aggregate rows={agg.get('rows')}"
        )

    parser = load_module("px_forecast_parser_v023", PARSER_SCRIPT)
    return parser, summary, parser_sha, builder_sha


def norm_symbol(code: Any) -> str | None:
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(digits) != 6:
        return None
    if digits.startswith(("5", "6", "9")):
        return digits + ".SH"
    if digits.startswith(("0", "1", "2", "3")):
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

    return df.sort_values(["event_date", "announcementId"]).reset_index(drop=True)


def source_pdf_for(source_root: Path, month: str, announcement_id: str) -> Path:
    return source_root / "chunks" / month / "pdf" / f"{announcement_id}.pdf"


def parse_one(r: pd.Series, pdf_path: Path, parser, pymupdf):
    title = str(r["announcementTitle"])
    text = parser.extract_pdf_text(pdf_path, pymupdf)
    section = parser.forecast_section(text)

    kind = parser.event_kind(title)
    explicit_st = bool(parser.ST_PAT.search(text[:800]))
    eligible = kind in {"ORIGINAL_FORECAST", "FORECAST_REVISION"} and not explicit_st

    period = parser.report_period(title, text)
    current = parser.parse_amount_after_anchor(section)

    # V0.2.3 semantics: explicit prior-period section first, loose table fallback second.
    prior = (
        parser.parse_prior_from_prior_section(text)
        or parser.parse_prior_from_table(section, current)
    )

    yoy = parser.parse_signed_yoy(section, current)
    change_type, change_basis = parser.derive_change(
        section, current, prior, yoy
    )

    c_lo_y = parser.to_yuan(current["lo"], current["unit"]) if current else None
    c_hi_y = parser.to_yuan(current["hi"], current["unit"]) if current else None
    p_lo_y = parser.to_yuan(prior["lo"], prior["unit"]) if prior else None
    p_hi_y = parser.to_yuan(prior["hi"], prior["unit"]) if prior else None

    derived_change_pct = None
    if None not in (c_lo_y, c_hi_y, p_lo_y, p_hi_y):
        cmid = (c_lo_y + c_hi_y) / 2
        pmid = (p_lo_y + p_hi_y) / 2
        if abs(pmid) > 1:
            derived_change_pct = (cmid - pmid) / abs(pmid) * 100

    conf = parser.confidence(
        eligible,
        period,
        current,
        change_type,
        prior=prior,
        signed_yoy=yoy,
        change_basis=change_basis,
    )

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
            kind if kind not in {"ORIGINAL_FORECAST", "FORECAST_REVISION"}
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
        "profit_state": parser.profit_state(c_lo_y, c_hi_y),
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
            str(k): int(v)
            for k, v in rdf["event_kind"].value_counts().to_dict().items()
        },
        "explicit_st_at_event_count": int(rdf["explicit_st_at_event"].sum()),
        "eligible_coverage": {
            "report_period_rate": rate(eligible["report_period"].notna()),
            "current_profit_rate": rate(
                eligible["current_profit_min_yuan"].notna()
            ),
            "prior_profit_rate": rate(
                eligible["prior_profit_min_yuan"].notna()
            ),
            "change_type_rate": rate(eligible["change_type"] != "UNKNOWN"),
            "high_confidence_rate": rate(
                eligible["parser_confidence"] == "HIGH"
            ),
        },
        "change_type_counts": {
            str(k): int(v)
            for k, v in eligible["change_type"].value_counts().to_dict().items()
        },
        "confidence_counts": {
            str(k): int(v)
            for k, v in rdf["parser_confidence"].value_counts().to_dict().items()
        },
    }


def write_parquet(df: pd.DataFrame, path: Path) -> None:
    con = duckdb.connect(":memory:")
    try:
        con.register("_df", df)
        con.execute(
            f"COPY _df TO '{qpath(path)}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        con.close()


def aggregate(out_root: Path, months: list[str]) -> dict:
    chunk_paths = [
        out_root / "chunks" / m / "semantic_events.parquet"
        for m in months
    ]
    expr = "[" + ",".join(f"'{qpath(p)}'" for p in chunk_paths) + "]"
    all_path = out_root / "semantic_events_all.parquet"

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
            raise RuntimeError(
                f"FAIL_CLOSED aggregate duplicate announcementIds={dup}"
            )

        monthly = con.execute(f"""
            SELECT
              strftime(
                try_cast(event_time AS TIMESTAMP), '%Y-%m'
              ) AS event_month,
              count(*) AS rows,
              sum(
                CASE WHEN strategy_eligible_semantics THEN 1 ELSE 0 END
              ) AS eligible_rows,
              avg(
                CASE
                  WHEN strategy_eligible_semantics
                       AND report_period IS NOT NULL THEN 1.0
                  WHEN strategy_eligible_semantics THEN 0.0
                END
              ) AS report_period_rate,
              avg(
                CASE
                  WHEN strategy_eligible_semantics
                       AND current_profit_min_yuan IS NOT NULL THEN 1.0
                  WHEN strategy_eligible_semantics THEN 0.0
                END
              ) AS current_profit_rate,
              avg(
                CASE
                  WHEN strategy_eligible_semantics
                       AND change_type <> 'UNKNOWN' THEN 1.0
                  WHEN strategy_eligible_semantics THEN 0.0
                END
              ) AS change_type_rate,
              avg(
                CASE
                  WHEN strategy_eligible_semantics
                       AND parser_confidence='HIGH' THEN 1.0
                  WHEN strategy_eligible_semantics THEN 0.0
                END
              ) AS high_confidence_rate
            FROM read_parquet('{qpath(all_path)}')
            GROUP BY 1
            ORDER BY 1
        """).df()
        monthly.to_csv(
            out_root / "monthly_quality.csv",
            index=False,
            encoding="utf-8-sig",
        )

        stats = con.execute(f"""
            SELECT
              count(*) AS rows,
              count(DISTINCT announcementId) AS ids,
              count(DISTINCT symbol) AS symbols,
              sum(
                CASE WHEN strategy_eligible_semantics THEN 1 ELSE 0 END
              ) AS eligible,
              min(event_time),
              max(event_time)
            FROM read_parquet('{qpath(all_path)}')
        """).fetchone()

        event_kind = dict(con.execute(f"""
            SELECT event_kind, count(*)
            FROM read_parquet('{qpath(all_path)}')
            GROUP BY 1 ORDER BY 1
        """).fetchall())

        change = dict(con.execute(f"""
            SELECT change_type, count(*)
            FROM read_parquet('{qpath(all_path)}')
            WHERE strategy_eligible_semantics
            GROUP BY 1 ORDER BY 1
        """).fetchall())

        conf = dict(con.execute(f"""
            SELECT parser_confidence, count(*)
            FROM read_parquet('{qpath(all_path)}')
            GROUP BY 1 ORDER BY 1
        """).fetchall())

        eligible_metrics = con.execute(f"""
            SELECT
              avg(CASE WHEN report_period IS NOT NULL THEN 1.0 ELSE 0.0 END),
              avg(CASE WHEN current_profit_min_yuan IS NOT NULL
                       THEN 1.0 ELSE 0.0 END),
              avg(CASE WHEN prior_profit_min_yuan IS NOT NULL
                       THEN 1.0 ELSE 0.0 END),
              avg(CASE WHEN change_type <> 'UNKNOWN'
                       THEN 1.0 ELSE 0.0 END),
              avg(CASE WHEN parser_confidence='HIGH'
                       THEN 1.0 ELSE 0.0 END)
            FROM read_parquet('{qpath(all_path)}')
            WHERE strategy_eligible_semantics
        """).fetchone()

        return {
            "rows": int(stats[0]),
            "announcement_ids": int(stats[1]),
            "symbols": int(stats[2]),
            "strategy_eligible_rows": int(stats[3]),
            "min_event_time": (
                str(stats[4]) if stats[4] is not None else None
            ),
            "max_event_time": (
                str(stats[5]) if stats[5] is not None else None
            ),
            "event_kind_counts": {
                str(k): int(v) for k, v in event_kind.items()
            },
            "change_type_counts": {
                str(k): int(v) for k, v in change.items()
            },
            "confidence_counts": {
                str(k): int(v) for k, v in conf.items()
            },
            "eligible_coverage": {
                "report_period_rate": (
                    float(eligible_metrics[0])
                    if eligible_metrics[0] is not None else None
                ),
                "current_profit_rate": (
                    float(eligible_metrics[1])
                    if eligible_metrics[1] is not None else None
                ),
                "prior_profit_rate": (
                    float(eligible_metrics[2])
                    if eligible_metrics[2] is not None else None
                ),
                "change_type_rate": (
                    float(eligible_metrics[3])
                    if eligible_metrics[3] is not None else None
                ),
                "high_confidence_rate": (
                    float(eligible_metrics[4])
                    if eligible_metrics[4] is not None else None
                ),
            },
            "semantic_events_all": str(all_path),
            "semantic_events_all_sha256": sha256_file(all_path),
        }
    finally:
        con.close()


def compare_to_v01(source_root: Path, out_root: Path) -> dict:
    old_path = source_root / "semantic_events_all.parquet"
    new_path = out_root / "semantic_events_all.parquet"
    if not old_path.exists():
        raise RuntimeError(f"Source aggregate missing: {old_path}")

    diff_path = out_root / "semantic_diff_vs_v0_1.csv"
    con = duckdb.connect(":memory:")
    try:
        counts = con.execute(f"""
            WITH o AS (
              SELECT * FROM read_parquet('{qpath(old_path)}')
            ),
            n AS (
              SELECT * FROM read_parquet('{qpath(new_path)}')
            ),
            j AS (
              SELECT
                n.announcementId,
                o.event_kind AS old_event_kind,
                n.event_kind AS new_event_kind,
                o.strategy_eligible_semantics AS old_eligible,
                n.strategy_eligible_semantics AS new_eligible,
                o.prior_profit_min_yuan AS old_prior_min,
                n.prior_profit_min_yuan AS new_prior_min,
                o.prior_profit_max_yuan AS old_prior_max,
                n.prior_profit_max_yuan AS new_prior_max,
                o.change_type AS old_change_type,
                n.change_type AS new_change_type,
                o.change_basis AS old_change_basis,
                n.change_basis AS new_change_basis,
                o.parser_confidence AS old_confidence,
                n.parser_confidence AS new_confidence
              FROM o
              FULL OUTER JOIN n USING (announcementId)
            )
            SELECT
              count(*) AS joined_rows,
              sum(CASE WHEN old_event_kind IS DISTINCT FROM new_event_kind
                       THEN 1 ELSE 0 END) AS event_kind_changed,
              sum(CASE WHEN old_eligible IS DISTINCT FROM new_eligible
                       THEN 1 ELSE 0 END) AS eligibility_changed,
              sum(CASE
                    WHEN old_prior_min IS DISTINCT FROM new_prior_min
                      OR old_prior_max IS DISTINCT FROM new_prior_max
                    THEN 1 ELSE 0
                  END) AS prior_changed,
              sum(CASE WHEN old_change_type IS DISTINCT FROM new_change_type
                       THEN 1 ELSE 0 END) AS change_type_changed,
              sum(CASE WHEN old_confidence IS DISTINCT FROM new_confidence
                       THEN 1 ELSE 0 END) AS confidence_changed
            FROM j
        """).fetchone()

        con.execute(f"""
            COPY (
              WITH o AS (
                SELECT * FROM read_parquet('{qpath(old_path)}')
              ),
              n AS (
                SELECT * FROM read_parquet('{qpath(new_path)}')
              )
              SELECT
                n.announcementId,
                n.symbol,
                n.secName,
                n.event_time,
                n.title,
                o.event_kind AS old_event_kind,
                n.event_kind AS new_event_kind,
                o.strategy_eligible_semantics AS old_eligible,
                n.strategy_eligible_semantics AS new_eligible,
                o.prior_profit_min_yuan AS old_prior_min_yuan,
                n.prior_profit_min_yuan AS new_prior_min_yuan,
                o.prior_profit_max_yuan AS old_prior_max_yuan,
                n.prior_profit_max_yuan AS new_prior_max_yuan,
                o.change_type AS old_change_type,
                n.change_type AS new_change_type,
                o.change_basis AS old_change_basis,
                n.change_basis AS new_change_basis,
                o.parser_confidence AS old_confidence,
                n.parser_confidence AS new_confidence
              FROM o
              JOIN n USING (announcementId)
              WHERE
                   o.event_kind IS DISTINCT FROM n.event_kind
                OR o.strategy_eligible_semantics
                   IS DISTINCT FROM n.strategy_eligible_semantics
                OR o.prior_profit_min_yuan
                   IS DISTINCT FROM n.prior_profit_min_yuan
                OR o.prior_profit_max_yuan
                   IS DISTINCT FROM n.prior_profit_max_yuan
                OR o.change_type IS DISTINCT FROM n.change_type
                OR o.change_basis IS DISTINCT FROM n.change_basis
                OR o.parser_confidence IS DISTINCT FROM n.parser_confidence
              ORDER BY n.event_time, n.announcementId
            )
            TO '{qpath(diff_path)}'
            (HEADER, DELIMITER ',')
        """)

        changed_rows = int(con.execute(f"""
            SELECT count(*) FROM read_csv_auto(
              '{qpath(diff_path)}', header=true
            )
        """).fetchone()[0])

    finally:
        con.close()

    return {
        "joined_rows": int(counts[0]),
        "changed_rows": changed_rows,
        "event_kind_changed": int(counts[1]),
        "eligibility_changed": int(counts[2]),
        "prior_changed": int(counts[3]),
        "change_type_changed": int(counts[4]),
        "confidence_changed": int(counts[5]),
        "diff_csv": str(diff_path),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="PositionX Forecast Corpus local reparse V0.2.3"
    )
    ap.add_argument("command", choices=["check", "run"])
    ap.add_argument("--events", default=str(EVENTS))
    ap.add_argument("--source-root", default=str(SOURCE_CORPUS_ROOT))
    ap.add_argument("--out-root", default=str(OUT_ROOT))
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    args = ap.parse_args()

    source_root = Path(args.source_root)
    out_root = Path(args.out_root)

    parser, source_summary, parser_sha, builder_sha = verify_dependencies(
        source_root
    )

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if end < start:
        raise RuntimeError("end < start")

    events_path = Path(args.events)
    if not events_path.exists():
        raise RuntimeError(f"events missing: {events_path}")

    df = load_events(events_path, start, end)
    plan = (
        df.groupby("event_month")
        .agg(
            events=("announcementId", "size"),
            symbols=("symbol", "nunique"),
        )
        .reset_index()
        .sort_values("event_month")
    )

    source_pdf_count = 0
    missing_examples = []
    for _, r in df.iterrows():
        p = source_pdf_for(
            source_root,
            str(r["event_month"]),
            str(r["announcementId"]),
        )
        if p.exists():
            source_pdf_count += 1
        elif len(missing_examples) < 20:
            missing_examples.append(str(p))

    print("FORECAST_CORPUS_LOCAL_REPARSE_V0_2_3_PLAN")
    print(
        f"events={len(df)} symbols={df['symbol'].nunique()} "
        f"months={len(plan)} source_pdfs={source_pdf_count}"
    )
    print(plan.to_string(index=False))
    print(
        json.dumps(
            {
                "parser_sha256": parser_sha,
                "base_builder_sha256": builder_sha,
                "source_corpus_status": source_summary.get("status"),
                "source_corpus_rows": (
                    (source_summary.get("aggregate") or {}).get("rows")
                ),
                "source_pdf_count": source_pdf_count,
                "missing_pdf_count": len(df) - source_pdf_count,
                "missing_examples": missing_examples,
                "network": False,
                "ocr": False,
                "source_corpus_mutation": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    if len(df) != 12090:
        raise RuntimeError(f"FAIL_CLOSED expected 12090 events, got {len(df)}")
    if source_pdf_count != len(df):
        raise RuntimeError(
            "FAIL_CLOSED not all source PDFs are present; "
            f"missing={len(df)-source_pdf_count}"
        )
    if len(plan) != 30:
        raise RuntimeError(
            f"FAIL_CLOSED expected 30 months, got {len(plan)}"
        )

    if args.command == "check":
        return 0

    chunks_root = out_root / "chunks"
    chunks_root.mkdir(parents=True, exist_ok=True)
    pymupdf = parser.select_pdf_engine()

    months = plan["event_month"].tolist()

    for month in months:
        final_dir = chunks_root / month
        manifest_path = final_dir / "chunk_manifest.json"

        if manifest_path.exists():
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                m.get("status") == "SUCCESS"
                and m.get("parser_sha256") == EXPECTED_PARSER_SHA256
            ):
                print(f"SKIP_COMMITTED {month}")
                continue
            raise RuntimeError(
                f"Existing incompatible/non-success manifest: {manifest_path}"
            )

        tmp = chunks_root / f".{month}.tmp"
        if tmp.exists():
            print(f"RESTART_PARTIAL {month} dir={tmp}")
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True, exist_ok=False)

        g = df[df["event_month"] == month].copy()
        rows = []
        errors = []

        print(f"REPARSE {month} events={len(g)}")

        for i, (_, r) in enumerate(g.iterrows(), start=1):
            ann_id = str(r["announcementId"])
            pdf = source_pdf_for(source_root, month, ann_id)

            try:
                if not pdf.exists():
                    raise RuntimeError(f"source PDF missing: {pdf}")
                parsed = parse_one(r, pdf, parser, pymupdf)
                rows.append(parsed)

                if i == 1 or i % 100 == 0 or i == len(g):
                    print(
                        f"  [{i}/{len(g)}] {ann_id} "
                        f"kind={parsed['event_kind']} "
                        f"change={parsed['change_type']} "
                        f"conf={parsed['parser_confidence']}"
                    )
            except Exception as e:
                errors.append(
                    {
                        "announcementId": ann_id,
                        "symbol": r.get("symbol"),
                        "pdf_path": str(pdf),
                        "error": f"{type(e).__name__}: {e}",
                    }
                )
                print(f"  ERROR {ann_id} {errors[-1]['error']}")

        write_json_atomic(tmp / "errors.json", errors)

        if errors:
            write_json_atomic(
                tmp / "chunk_manifest.json",
                {
                    "status": "FAILED_LOCAL_REPARSE",
                    "event_month": month,
                    "expected_events": int(len(g)),
                    "parsed_events": int(len(rows)),
                    "error_count": int(len(errors)),
                    "parser_sha256": EXPECTED_PARSER_SHA256,
                    "source_corpus_root": str(source_root),
                },
            )
            raise RuntimeError(
                f"FAIL_CLOSED month={month} errors={len(errors)}; "
                f"evidence preserved at {tmp}"
            )

        rdf = pd.DataFrame(rows)
        if len(rdf) != len(g) or rdf["announcementId"].nunique() != len(g):
            raise RuntimeError(
                f"FAIL_CLOSED month={month} row/id mismatch "
                f"expected={len(g)} rows={len(rdf)} "
                f"ids={rdf['announcementId'].nunique()}"
            )

        pq = tmp / "semantic_events.parquet"
        write_parquet(rdf, pq)
        chunk_summary = summarize_chunk(rdf)

        manifest = {
            "status": "SUCCESS",
            "schema_version": "FORECAST_SEMANTIC_CORPUS_V023",
            "event_month": month,
            "created_at": datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            "parser_sha256": EXPECTED_PARSER_SHA256,
            "source_corpus_root": str(source_root),
            "source_pdf_mode": "REUSE_READ_ONLY",
            "semantic_events_sha256": sha256_file(pq),
            "summary": chunk_summary,
            "principles": {
                "canonical_forecast_metadata_source": True,
                "sh_sz_only": True,
                "bse_excluded": True,
                "explicit_st_at_event_excluded_from_strategy": True,
                "subsidiary_followup_excluded_from_strategy": True,
                "source_pdf_read_only": True,
                "no_network": True,
                "no_ocr": True,
                "no_database_mutation": True,
            },
        }
        write_json_atomic(tmp / "chunk_manifest.json", manifest)
        os.replace(tmp, final_dir)

        print(
            f"COMMITTED {month} rows={chunk_summary['rows']} "
            f"eligible={chunk_summary['eligible_rows']} "
            f"high={chunk_summary['eligible_coverage']['high_confidence_rate']}"
        )

    aggregate_summary = aggregate(out_root, months)
    diff_summary = compare_to_v01(source_root, out_root)

    summary = {
        "status": "PASS_FORECAST_SEMANTIC_CORPUS_LOCAL_REPARSE_V0_2_3",
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "requested_start": str(start),
        "requested_end": str(end),
        "parser_sha256": EXPECTED_PARSER_SHA256,
        "base_builder_sha256": EXPECTED_BASE_BUILDER_SHA256,
        "source_corpus_root": str(source_root),
        "source_corpus_status": source_summary.get("status"),
        "months_committed": len(months),
        "aggregate": aggregate_summary,
        "diff_vs_v0_1": diff_summary,
        "strategy_readiness_gate": {
            "automatic": (
                aggregate_summary["eligible_coverage"]["current_profit_rate"]
                >= 0.90
                and aggregate_summary["eligible_coverage"]["change_type_rate"]
                >= 0.80
                and aggregate_summary["eligible_coverage"]["high_confidence_rate"]
                >= 0.75
            ),
            "manual_review_required": True,
            "manual_review_command": (
                "python scripts\\audit_forecast_semantic_corpus_manual_gate_v02.py "
                f'--root "{out_root}"'
            ),
            "next_if_pass": "EARNINGS_DRIFT_STRATEGY_AUDIT_V0_1",
        },
        "network": False,
        "no_ocr": True,
        "no_database_mutation": True,
        "source_corpus_mutation": False,
        "output_dir": str(out_root),
        "outputs": [
            "semantic_events_all.parquet",
            "monthly_quality.csv",
            "semantic_diff_vs_v0_1.csv",
            "corpus_summary.json",
        ],
    }

    write_json_atomic(out_root / "corpus_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
