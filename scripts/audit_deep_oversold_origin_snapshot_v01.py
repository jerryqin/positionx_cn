from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_SCRIPTS = [
    ROOT / "scripts" / "run_deep_oversold_csi500_universe.py",
    ROOT / "payload" / "scripts" / "run_deep_oversold_csi500_universe.py",
]
RESULT_JSON = ROOT / "data" / "research" / "deep_oversold_csi500_universe_latest.json"
OUT_DIR = ROOT / "data" / "research" / "edge_discovery" / "deep_oversold_origin_snapshot_v01"

ANCHORS = {
    "trade_population": [
        "trades", "events", "standard_fillable", "signal_ret20",
        "entry_date", "exit_date",
    ],
    "portfolio": [
        "max_gross", "target_weight", "avg_gross_weight",
        "cagr_per_avg_gross", "round_trip_cost",
    ],
    "simulation": [
        "simulate", "backtest", "equity", "drawdown", "sharpe",
    ],
    "universe": [
        "csi500", "universe", "pit", "members",
    ],
}


def choose_script() -> Path:
    existing = [p for p in CANDIDATE_SCRIPTS if p.exists()]
    if not existing:
        raise FileNotFoundError(
            "run_deep_oversold_csi500_universe.py was not found in "
            "scripts/ or payload/scripts/."
        )
    # Prefer the live script over payload copy.
    return existing[0]


def node_source(lines: list[str], node: ast.AST) -> str:
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    if start is None or end is None:
        return ""
    return "\n".join(
        f"{i+1:04d}: {lines[i]}"
        for i in range(start - 1, min(end, len(lines)))
    )


def function_profiles(source: str) -> list[dict]:
    tree = ast.parse(source)
    lines = source.splitlines()
    out = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        body = ast.get_source_segment(source, node) or ""
        low = body.lower()
        matched = {
            group: [a for a in anchors if a.lower() in low]
            for group, anchors in ANCHORS.items()
        }
        score = sum(len(v) for v in matched.values())

        if score == 0:
            continue

        out.append({
            "name": node.name,
            "line_start": node.lineno,
            "line_end": getattr(node, "end_lineno", node.lineno),
            "score": score,
            "matched": matched,
            "source": node_source(lines, node),
        })

    out.sort(key=lambda x: (-x["score"], x["line_start"]))
    return out


def top_level_anchor_windows(source: str) -> list[dict]:
    lines = source.splitlines()
    hits = []

    flat = sorted({
        anchor.lower()
        for anchors in ANCHORS.values()
        for anchor in anchors
    })

    for i, line in enumerate(lines):
        low = line.lower()
        matched = [a for a in flat if a in low]
        if not matched:
            continue

        lo = max(0, i - 4)
        hi = min(len(lines), i + 5)
        hits.append({
            "line": i + 1,
            "matched": matched,
            "context": "\n".join(
                f"{j+1:04d}: {lines[j]}"
                for j in range(lo, hi)
            ),
        })

    # Deduplicate overlapping windows by line proximity.
    compact = []
    last_line = -999
    for h in hits:
        if h["line"] - last_line <= 4:
            continue
        compact.append(h)
        last_line = h["line"]
    return compact


def json_shape(obj, path="$", depth=0, max_depth=4):
    rows = []
    if depth > max_depth:
        return rows

    if isinstance(obj, dict):
        rows.append({
            "path": path,
            "type": "dict",
            "keys": list(obj.keys()),
        })
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                rows.extend(
                    json_shape(v, f"{path}.{k}", depth + 1, max_depth)
                )
    elif isinstance(obj, list):
        rows.append({
            "path": path,
            "type": "list",
            "length": len(obj),
        })
        if obj and isinstance(obj[0], (dict, list)):
            rows.extend(
                json_shape(obj[0], f"{path}[0]", depth + 1, max_depth)
            )
    return rows


def find_metric_paths(obj, path="$"):
    hits = []

    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            kl = str(k).lower()
            if any(
                token in kl
                for token in (
                    "trade", "cagr", "drawdown", "sharpe",
                    "gross", "capacity", "round_trip",
                )
            ):
                if not isinstance(v, (dict, list)):
                    hits.append({"path": p, "value": v})
            hits.extend(find_metric_paths(v, p))

    elif isinstance(obj, list):
        for i, v in enumerate(obj[:20]):
            hits.extend(find_metric_paths(v, f"{path}[{i}]"))

    return hits


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    script_path = choose_script()
    source = script_path.read_text(encoding="utf-8")
    profiles = function_profiles(source)
    windows = top_level_anchor_windows(source)

    result_payload = None
    shape = []
    metric_paths = []

    if RESULT_JSON.exists():
        result_payload = json.loads(
            RESULT_JSON.read_text(encoding="utf-8")
        )
        shape = json_shape(result_payload)
        metric_paths = find_metric_paths(result_payload)

    report = {
        "audit_id": "DEEP_OVERSOLD_ORIGIN_SNAPSHOT_V01",
        "script_path": str(script_path),
        "script_line_count": len(source.splitlines()),
        "relevant_functions": [
            {
                k: v
                for k, v in p.items()
                if k != "source"
            }
            for p in profiles
        ],
        "result_json": str(RESULT_JSON),
        "result_json_exists": RESULT_JSON.exists(),
        "result_json_shape": shape,
        "metric_paths": metric_paths,
    }

    (OUT_DIR / "audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with (OUT_DIR / "relevant_source.txt").open("w", encoding="utf-8") as f:
        f.write(f"SOURCE: {script_path}\n")
        f.write("=" * 110 + "\n\n")

        for p in profiles:
            f.write(
                f"FUNCTION {p['name']} "
                f"lines {p['line_start']}-{p['line_end']} "
                f"score={p['score']}\n"
            )
            f.write(
                "matched="
                + json.dumps(p["matched"], ensure_ascii=False)
                + "\n"
            )
            f.write("-" * 110 + "\n")
            f.write(p["source"] + "\n\n")

    with (OUT_DIR / "anchor_windows.txt").open("w", encoding="utf-8") as f:
        for w in windows:
            f.write("=" * 110 + "\n")
            f.write(
                f"line={w['line']} matched={w['matched']}\n"
            )
            f.write(w["context"] + "\n")

    print("=" * 110)
    print("DEEP OVERSOLD CSI500 ORIGIN SNAPSHOT V0.1")
    print("=" * 110)
    print("Source:", script_path)
    print("Lines:", len(source.splitlines()))
    print()

    print("MOST RELEVANT FUNCTIONS")
    print("-" * 110)
    for p in profiles[:12]:
        print(
            f"{p['name']}: lines {p['line_start']}-{p['line_end']} "
            f"score={p['score']}"
        )
        groups = []
        for group, vals in p["matched"].items():
            if vals:
                groups.append(f"{group}={','.join(vals)}")
        print("  " + " | ".join(groups))
    print()

    print("RESULT JSON METRIC PATHS")
    print("-" * 110)
    if not metric_paths:
        print("NONE / result json not found")
    else:
        for item in metric_paths[:120]:
            print(f"{item['path']} = {item['value']}")
    print()

    print("SOURCE EXCERPTS TO PASTE BACK")
    print("-" * 110)
    for p in profiles[:6]:
        print(
            f"\n### FUNCTION {p['name']} "
            f"({p['line_start']}-{p['line_end']})\n"
        )
        print(p["source"])
        print()

    print("Output:", OUT_DIR)
    print("No strategy/research data was modified.")


if __name__ == "__main__":
    main()
