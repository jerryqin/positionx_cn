from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Dict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from xtquant import xtdata

from research.strategy_matching import BreakoutRetestMatcher, LevelHoldMatcher, MA60SupportMatcher
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, save_watchlist_snapshot


def load_levels(path: Path) -> Dict[str, float]:
    if not path.exists():
        return {}
    obj = json.loads(path.read_text(encoding="utf-8"))
    raw = obj.get("levels", obj)
    return {str(k): float(v) for k, v in raw.items() if float(v) > 0}


def main() -> int:
    p = argparse.ArgumentParser(description="PositionX Strategy Matching v1 — research only")
    p.add_argument("--watchlist", default="data/research/watchlist_latest.json")
    p.add_argument("--levels", default="data/research/levels.json")
    p.add_argument("--bars", type=int, default=260)
    p.add_argument(
        "--refresh-history", action="store_true",
        help="首次启用MA200时补充更长日K缓存；日常运行无需此参数",
    )
    p.add_argument(
        "--history-calendar-days", type=int, default=550,
        help="--refresh-history 时补充的自然日窗口，默认550",
    )
    p.add_argument("--output", default="data/research/strategy_matches_latest.json")
    args = p.parse_args()

    watchlist_path = PROJECT_ROOT / args.watchlist
    if not watchlist_path.exists():
        raise SystemExit(
            f"找不到 {watchlist_path}; 请先运行 scripts/run_watchlist.py 生成 v1.2 candidate_pool"
        )

    payload = json.loads(watchlist_path.read_text(encoding="utf-8"))
    pool = payload.get("candidate_pool")
    if not isinstance(pool, list):
        raise SystemExit("watchlist_latest.json 中没有 candidate_pool；请先升级/运行 Watchlist v1.2")

    symbols = sorted({str(r["symbol"]) for r in pool if r.get("symbol")})
    levels = load_levels(PROJECT_ROOT / args.levels)

    adapter = XtDataWatchlistAdapter(
        xtdata,
        AdapterConfig(sector="", bars=args.bars, dividend_type="front"),
    )
    if args.refresh_history:
        adapter.refresh_history(symbols, calendar_days=args.history_calendar_days)

    history = adapter.history(symbols)

    breakout = BreakoutRetestMatcher()
    level_hold = LevelHoldMatcher()
    ma60_support = MA60SupportMatcher()

    results = []
    for symbol in symbols:
        df = history.get(symbol)
        if df is None:
            continue

        br = breakout.match(symbol, df)
        if br["matched"] or br["state"] == "BREAKOUT_FAILED":
            results.append(br)

        if symbol in levels:
            lh = level_hold.match(symbol, df, levels[symbol])
            results.append(lh)

        ms = ma60_support.match(symbol, df)
        if ms["matched"]:
            results.append(ms)

    matched = [r for r in results if r["matched"]]
    triggered = [r for r in results if r["triggered"]]
    failed = [r for r in results if r["state"] == "BREAKOUT_FAILED"]
    state_counts = {}
    for r in results:
        state = str(r.get("state", "UNKNOWN"))
        state_counts[state] = state_counts.get(state, 0) + 1

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "RESEARCH_STRATEGY_MATCHING_ONLY",
        "safety": {
            "uses_broker": False,
            "production_db_opened": False,
            "creates_trading_plan": False,
            "can_submit_order": False,
        },
        "input": {
            "candidate_pool_symbols": len(symbols),
            "history_loaded": len(history),
            "configured_level_symbols": len(levels),
            "bars": args.bars,
            "history_refreshed": bool(args.refresh_history),
            "history_calendar_days": int(args.history_calendar_days) if args.refresh_history else None,
        },
        "summary": {
            "result_rows": len(results),
            "matched_total": len(matched),
            "triggered_total": len(triggered),
            "breakout_retest_matched": sum(
                1 for r in matched if r["strategy"] == "BREAKOUT_RETEST"
            ),
            "breakout_retest_triggered": sum(
                1 for r in triggered if r["strategy"] == "BREAKOUT_RETEST"
            ),
            "level_hold_matched": sum(
                1 for r in matched if r["strategy"] == "LEVEL_HOLD"
            ),
            "ma60_support_matched": sum(
                1 for r in matched if r["strategy"] == "MA60_SUPPORT"
            ),
            "ma60_support_triggered": sum(
                1 for r in triggered if r["strategy"] == "MA60_SUPPORT"
            ),
            "breakout_failed": len(failed),
            "state_counts": state_counts,
        },
        "matches": results,
    }

    path = save_watchlist_snapshot(PROJECT_ROOT / args.output, out)

    print(json.dumps(out["safety"], ensure_ascii=False, indent=2))
    print(json.dumps(out["input"], ensure_ascii=False, indent=2))
    print(json.dumps(out["summary"], ensure_ascii=False, indent=2))
    print(f"OUTPUT: {path}")

    print("\nMATCHED / MONITORED:")
    for r in matched[:50]:
        ev = r.get("evidence", {})
        level = ev.get("active_breakout_level", ev.get("level", ev.get("ma60")))
        event_date = ev.get("breakout_date", ev.get("latest_date"))
        print(
            f"{r['strategy']:16s} {r['symbol']:10s} "
            f"state={r['state']:14s} triggered={str(r['triggered']):5s} "
            f"level={level} date={event_date} reason={r['reason']}"
        )

    if failed:
        print("\nRECENT BREAKOUT FAILURES:")
        for r in failed[:50]:
            ev = r.get("evidence", {})
            print(
                f"{r['symbol']:10s} "
                f"level={ev.get('active_breakout_level')} "
                f"breakout_date={ev.get('breakout_date')} "
                f"latest_date={ev.get('latest_date')} "
                f"reason={r['reason']}"
            )

    if triggered:
        print("\nTRIGGERED NOW:")
        for r in triggered[:50]:
            ev = r.get("evidence", {})
            level = ev.get("active_breakout_level", ev.get("level", ev.get("ma60")))
            event_date = ev.get("breakout_date", ev.get("latest_date"))
            print(
                f"{r['strategy']:16s} {r['symbol']:10s} "
                f"state={r['state']} level={level} "
                f"date={event_date} latest_date={ev.get('latest_date')}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
