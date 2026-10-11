from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from typing import Any

import duckdb


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DIR = ROOT / "data" / "research"
OUT_DIR = RESEARCH_DIR / "edge_discovery" / "deep_oversold_metric_provenance_v01"

TARGET_TOKENS = [
    "1365",
    "11.04",
    "11.040",
    "9.62",
    "9.620",
    "1.042",
    "11.82",
    "11.820",
    "93.39",
    "93.390",
]

SOURCE_TOKENS = [
    "deep_oversold",
    "Deep Oversold",
    "CSI500",
    "csi500",
    "max_gross",
    "target_weight",
    "standard_fillable",
    "first signal",
    "first_signal",
    "duplicate",
    "cap_eff",
    "capacity_eff",
    "avg_gross",
]

TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".csv", ".tsv",
}

SKIP_DIRS = {
    ".git", ".venv", "__pycache__", "node_modules",
}


def should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def safe_read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > 50 * 1024 * 1024:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def line_hits(path: Path, text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    hits = []

    for i, line in enumerate(lines):
        matched_targets = [t for t in TARGET_TOKENS if t in line]
        matched_sources = [t for t in SOURCE_TOKENS if t in line]

        if not matched_targets and not matched_sources:
            continue

        lo = max(0, i - 3)
        hi = min(len(lines), i + 4)

        context = "\n".join(
            f"{j+1}: {lines[j]}"
            for j in range(lo, hi)
        )

        hits.append({
            "path": str(path),
            "line": i + 1,
            "matched_targets": matched_targets,
            "matched_source_tokens": matched_sources,
            "context": context,
        })

    return hits


def deep_find_numeric(obj: Any, path: str = "$") -> list[dict[str, Any]]:
    hits = []

    if isinstance(obj, dict):
        for k, v in obj.items():
            hits.extend(deep_find_numeric(v, f"{path}.{k}"))
        return hits

    if isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(deep_find_numeric(v, f"{path}[{i}]"))
        return hits

    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        targets = {
            1365.0: "1365",
            0.1104: "11.04%",
            11.04: "11.04",
            -0.0962: "-9.62%",
            -9.62: "-9.62",
            1.042: "1.042",
            0.1182: "11.82%",
            11.82: "11.82",
            0.9339: "93.39%",
            93.39: "93.39",
        }
        for target, label in targets.items():
            tol = max(1e-9, abs(target) * 1e-5)
            if math.isclose(float(obj), target, rel_tol=0, abs_tol=tol):
                hits.append({
                    "json_path": path,
                    "value": obj,
                    "matched": label,
                })

    return hits


def inspect_json(path: Path, text: str) -> list[dict[str, Any]]:
    try:
        obj = json.loads(text)
    except Exception:
        return []

    return deep_find_numeric(obj)


def inspect_parquet(path: Path) -> dict[str, Any]:
    q = path.as_posix().replace("'", "''")
    con = duckdb.connect()
    try:
        desc = con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{q}')"
        ).fetchall()
        cols = [str(r[0]) for r in desc]
        row_count = int(
            con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{q}')"
            ).fetchone()[0]
        )
        return {
            "path": str(path),
            "rows": row_count,
            "columns": cols,
        }
    except Exception as exc:
        return {
            "path": str(path),
            "error": repr(exc),
        }
    finally:
        con.close()


def scan_repository() -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    text_hits = []
    json_hits = []
    parquet_profiles = []

    for path in ROOT.rglob("*"):
        if not path.is_file() or should_skip(path):
            continue

        suffix = path.suffix.lower()
        lname = path.as_posix().lower()

        if suffix in TEXT_SUFFIXES:
            text = safe_read_text(path)
            if text is None:
                continue

            hits = line_hits(path, text)
            text_hits.extend(hits)

            if suffix == ".json":
                jhits = inspect_json(path, text)
                for h in jhits:
                    h["path"] = str(path)
                json_hits.extend(jhits)

        if suffix == ".parquet" and "deep" in lname and "oversold" in lname:
            parquet_profiles.append(inspect_parquet(path))

    return text_hits, json_hits, parquet_profiles


def source_code_focus(text_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Rank code/report hits that are most likely to explain the frozen CSI500
    economics rather than merely mention Deep Oversold.
    """
    scored = []

    for h in text_hits:
        p = h["path"].lower()
        score = 0

        if p.endswith(".py"):
            score += 5
        if p.endswith(".json") or p.endswith(".md"):
            score += 2

        targets = h["matched_targets"]
        sources = h["matched_source_tokens"]

        score += 5 * len(targets)
        score += 1 * len(sources)

        context = h["context"].lower()

        for token in (
            "cagr", "drawdown", "sharpe", "trade", "avg_gross",
            "capacity", "csi500", "1365",
        ):
            if token in context:
                score += 2

        if "edge_discovery" in p:
            score += 1

        if score > 0:
            x = dict(h)
            x["score"] = score
            scored.append(x)

    scored.sort(
        key=lambda x: (-x["score"], x["path"], x["line"])
    )
    return scored


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    text_hits, json_hits, parquet_profiles = scan_repository()
    focused = source_code_focus(text_hits)

    # Files that actually mention numeric targets.
    numeric_files = {}
    for h in text_hits:
        if h["matched_targets"]:
            numeric_files.setdefault(h["path"], set()).update(
                h["matched_targets"]
            )
    for h in json_hits:
        numeric_files.setdefault(h["path"], set()).add(
            h["matched"]
        )

    report = {
        "audit_id": "DEEP_OVERSOLD_METRIC_PROVENANCE_V01",
        "targets": {
            "trades": 1365,
            "cagr": "11.04%",
            "mdd": "-9.62%",
            "sharpe": 1.042,
            "avg_gross": "11.82%",
            "capacity_efficiency": "93.39",
        },
        "numeric_files": [
            {
                "path": p,
                "matched": sorted(list(tokens)),
            }
            for p, tokens in sorted(numeric_files.items())
        ],
        "top_focused_hits": focused[:200],
        "json_numeric_hits": json_hits[:500],
        "deep_oversold_parquet_profiles": parquet_profiles,
        "notes": [
            "This audit does not assume 1365 is a regime-event count.",
            "Its purpose is to locate the artifact/code lineage that originally produced the frozen CSI500 economics.",
        ],
    }

    (OUT_DIR / "audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with (OUT_DIR / "top_hits.txt").open("w", encoding="utf-8") as f:
        for h in focused[:200]:
            f.write("=" * 100 + "\n")
            f.write(
                f"score={h['score']} path={h['path']} line={h['line']}\n"
            )
            if h["matched_targets"]:
                f.write(
                    "targets=" + ",".join(h["matched_targets"]) + "\n"
                )
            f.write(h["context"] + "\n")

    print("=" * 110)
    print("DEEP OVERSOLD CSI500 METRIC PROVENANCE AUDIT V0.1")
    print("=" * 110)
    print(
        "Targets: trades=1365 | CAGR≈11.04% | MDD≈-9.62% | "
        "Sharpe≈1.042 | avgGross≈11.82% | capEff≈93.39"
    )
    print()

    print("FILES CONTAINING TARGET NUMBERS")
    print("-" * 110)
    if not numeric_files:
        print("NONE")
    else:
        for p, tokens in sorted(numeric_files.items()):
            print(f"{p}")
            print("  matched:", ", ".join(sorted(tokens)))
    print()

    print("TOP PROVENANCE HITS")
    print("-" * 110)
    if not focused:
        print("NONE")
    else:
        for h in focused[:40]:
            print(
                f"[score={h['score']}] {h['path']}:{h['line']}"
            )
            print(h["context"])
            print()

    print("DEEP OVERSOLD PARQUET FILES")
    print("-" * 110)
    for p in parquet_profiles:
        if "error" in p:
            print(p["path"], "| ERROR", p["error"])
        else:
            cols = p["columns"]
            preview = ", ".join(cols[:18])
            if len(cols) > 18:
                preview += ", ..."
            marker = "  <== ROWS=1365" if p["rows"] == 1365 else ""
            print(
                f"{p['path']} | rows={p['rows']}{marker}"
            )
            print("  columns:", preview)

    print()
    print("Output:", OUT_DIR)
    print("No source files or databases were modified.")


if __name__ == "__main__":
    main()
