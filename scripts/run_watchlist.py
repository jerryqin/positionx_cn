from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Dict

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.service import TradingService
from research.watchlist import WatchlistConfig, WatchlistEngine
from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter, connected_xtdata, save_watchlist_snapshot


def _forbidden_action(*args, **kwargs):
    raise RuntimeError("WATCHLIST READ-ONLY GUARD: broker write action is disabled")


def _isolated_settings(source: Path, temp_dir: Path) -> Path:
    cfg: Dict[str, Any] = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    cfg.setdefault("app", {})["db_path"] = str(temp_dir / "watchlist_readonly.db")
    cfg.setdefault("execution", {})["live_enabled"] = False
    cfg["execution"]["auto_execute"] = False
    out = temp_dir / "settings.watchlist.yaml"
    out.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="PositionX Watchlist Engine v1 read-only runner")
    p.add_argument("--settings", default="config/settings.yaml")
    p.add_argument("--sector", default="沪深300")
    p.add_argument("--bars", type=int, default=80)
    p.add_argument("--top-n", type=int, default=20)
    p.add_argument("--sample", type=int, default=0, help="0=全部；>0仅取前N个成分用于接线验证")
    p.add_argument("--refresh-sector", action="store_true")
    p.add_argument("--refresh-history", action="store_true")
    p.add_argument("--output", default="data/research/watchlist_latest.json")
    args = p.parse_args()

    source = Path(args.settings).resolve()
    if not source.exists():
        raise SystemExit(f"找不到配置: {source}")

    service = None
    with tempfile.TemporaryDirectory(prefix="positionx_watchlist_") as td:
        isolated = _isolated_settings(source, Path(td))
        service = TradingService(str(isolated))
        if service.broker is None:
            raise RuntimeError("qmt.account_id 为空，无法自动合并真实持仓")

        # Independent hard guard: this research process cannot submit/cancel.
        service.broker.submit_limit = _forbidden_action
        service.broker.cancel = _forbidden_action

        try:
            service.connect()
            positions = service.broker.query_positions()
            holdings = sorted({p.symbol for p in positions if int(p.volume or 0) > 0})

            # Market data is lazy-initialized; connect it explicitly before access.
            xtdata = connected_xtdata(service.market)
            adapter = XtDataWatchlistAdapter(
                xtdata,
                AdapterConfig(sector=args.sector, bars=args.bars, dividend_type="front"),
            )
            universe = adapter.universe(refresh_sector=args.refresh_sector)
            if args.sample > 0:
                universe = universe[: args.sample]

            symbols = adapter.merge_holdings(universe, holdings)

            if args.refresh_history:
                adapter.refresh_history(symbols)

            history = adapter.history(symbols)
            engine = WatchlistEngine(
                WatchlistConfig(
                    min_bars=60,
                    top_n=args.top_n,
                    min_price=2.0,
                    min_avg_amount20=50_000_000.0,
                    min_ret20=0.0,
                    near_high20_ratio=0.90,
                    require_trend=True,
                )
            )
            rows = engine.build(
                history,
                holdings=holdings,
                ranking_universe=universe,
            )
            candidate_pool = engine.build(
                history,
                holdings=holdings,
                ranking_universe=universe,
                mode="candidate_pool",
            )

            payload = {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "mode": "RESEARCH_WATCHLIST_ONLY",
                "safety": {
                    "production_db_opened": False,
                    "broker_submit_guard": "ENABLED",
                    "broker_cancel_guard": "ENABLED",
                    "creates_trading_plan": False,
                },
                "universe": {
                    "sector": args.sector,
                    "sector_members_loaded": len(universe),
                    "holdings": holdings,
                    "combined_symbols": len(symbols),
                    "history_loaded": len(history),
                },
                "summary": {
                    "selected_total": len(rows),
                    "holdings_retained": sum(1 for r in rows if r["status"] == "HOLDING"),
                    "auto_selected": sum(1 for r in rows if r["status"] == "SELECTED"),
                    "pinned": sum(1 for r in rows if r["status"] == "PINNED"),
                    "candidate_pool_total": len(candidate_pool),
                    "candidate_pool_auto": sum(
                        1 for r in candidate_pool if r["status"] == "CANDIDATE"
                    ),
                },
                "watchlist": rows,
                "candidate_pool": candidate_pool,
            }

            out = save_watchlist_snapshot(PROJECT_ROOT / args.output, payload)
            print(json.dumps(payload["safety"], ensure_ascii=False, indent=2))
            print(json.dumps(payload["universe"], ensure_ascii=False, indent=2))
            print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
            print(f"OUTPUT: {out}")
            print("\nTOP WATCHLIST:")
            for row in rows[: min(20, len(rows))]:
                score_text = (
                    f"{row['score']:6.2f}"
                    if row["score"] is not None
                    else "   N/A"
                )
                print(
                    f"{row['status']:8s} {row['symbol']:10s} "
                    f"score={score_text} "
                    f"reasons={'; '.join(row['reasons'][:4])}"
                )
            return 0
        finally:
            if service is not None:
                try:
                    service.disconnect()
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
