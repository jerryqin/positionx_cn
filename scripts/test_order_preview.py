from __future__ import annotations

import argparse
from dataclasses import replace
import json
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict

import yaml

# Make D:\\positionx_min_live importable when this file lives in scripts/.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.execution import ExecutionEngine
from core.models import TradingPlanItem
from core.service import TradingService


TEST_STRATEGY_NAME = "ISOLATED_ORDER_PREVIEW"


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _first_number(value) -> float:
    if isinstance(value, (list, tuple)) and value:
        return float(value[0] or 0)
    try:
        if hasattr(value, "__len__") and not isinstance(value, (str, bytes)) and len(value):
            return float(value[0] or 0)
    except Exception:
        pass
    return 0.0



def _tick_audit(market, tick: Dict[str, Any], stale_seconds: int) -> Dict[str, Any]:
    # Audit the exact tick used by ORDER_PREVIEW.
    # tick_fresh delegates to production MarketDataService.is_tick_fresh().
    raw_time = tick.get("time") or tick.get("timestamp")
    tick_dt = None

    if raw_time is not None:
        try:
            ts = float(raw_time)
            if ts > 1e12:
                ts /= 1000.0
            tick_dt = datetime.fromtimestamp(ts)
        except (TypeError, ValueError, OSError, OverflowError):
            tick_dt = None

    # Display fallback only; freshness PASS/FAIL still comes from is_tick_fresh().
    if tick_dt is None:
        try:
            tick_dt = market._tick_datetime(tick)
        except Exception:
            tick_dt = None

    age_seconds = None
    if tick_dt is not None:
        age_seconds = round((datetime.now() - tick_dt).total_seconds(), 3)

    return {
        "tick_time": tick_dt.strftime("%Y-%m-%d %H:%M:%S") if tick_dt else None,
        "tick_age_seconds": age_seconds,
        "stale_tick_seconds": int(stale_seconds),
        "tick_fresh": bool(market.is_tick_fresh(tick, int(stale_seconds))),
        "tick_time_raw": raw_time,
        "timetag_raw": tick.get("timetag"),
    }


def _load_isolated_config(source: Path, temp_dir: Path, symbol: str) -> Path:
    with source.open("r", encoding="utf-8") as f:
        cfg: Dict[str, Any] = yaml.safe_load(f) or {}

    # Hard isolation: never point at the production SQLite database.
    cfg.setdefault("app", {})["db_path"] = str(temp_dir / "order_preview_test.db")

    # Use the requested symbol as the live-session reference and market source.
    cfg.setdefault("market", {})["symbols"] = [symbol]

    # Hard no-submit guards, even if production config has later been changed.
    cfg.setdefault("execution", {})["live_enabled"] = False
    cfg["execution"]["auto_execute"] = False

    out = temp_dir / "settings.order_preview.yaml"
    with out.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    return out


def _forbidden_action(*args, **kwargs):
    raise RuntimeError("ISOLATED ORDER_PREVIEW safety guard: broker write action is disabled")


def _position_row(position, total_asset: float) -> Dict[str, Any]:
    if position is None:
        return {
            "symbol": None,
            "volume": 0,
            "can_use_volume": 0,
            "market_value": 0.0,
            "current_weight": 0.0,
        }
    mv = float(position.market_value or 0.0)
    return {
        "symbol": position.symbol,
        "volume": int(position.volume),
        "can_use_volume": int(position.can_use_volume),
        "market_value": mv,
        "current_weight": (mv / total_asset) if total_asset > 0 else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "PositionX isolated ORDER_PREVIEW test. Reads real miniQMT account/market data, "
            "uses a temporary SQLite DB, and can never submit/cancel orders."
        )
    )
    parser.add_argument("--symbol", default="603986.SH", help="测试标的，例如 603986.SH")
    parser.add_argument("--target", type=float, default=None, help="TARGET_WEIGHT 模式：目标仓位 0~1；未指定其它数量模式时默认 0.20")
    parser.add_argument("--target-shares", type=int, default=None, help="TARGET_SHARES 模式：目标最终持股数，例如 2100")
    parser.add_argument("--side", choices=["BUY", "SELL"], default=None, help="EXACT_VOLUME 模式方向")
    parser.add_argument("--volume", type=int, default=None, help="EXACT_VOLUME 模式精确股数")
    parser.add_argument("--settings", default="config/settings.yaml", help="正式配置文件，只读取；默认 config/settings.yaml")
    parser.add_argument("--keep-temp", action="store_true", help="保留临时目录用于审计；默认运行结束自动删除")
    parser.add_argument(
        "--engine-path",
        choices=["dry-run", "execute-guard"],
        default="dry-run",
        help=(
            "dry-run=调用 execute_plan(..., dry_run=True)；"
            "execute-guard=调用真实execute路径 dry_run=False，但隔离配置强制live_enabled=false且broker写操作硬禁用"
        ),
    )
    parser.add_argument(
        "--block-case",
        choices=["none", "kill-switch", "external-order", "no-sellable", "no-cash"],
        default="none",
        help="隔离模拟BLOCKED场景；默认none",
    )
    args = parser.parse_args()

    has_target = args.target is not None
    has_target_shares = args.target_shares is not None
    has_exact_volume = args.side is not None or args.volume is not None
    if has_exact_volume and (args.side is None or args.volume is None):
        parser.error("--side 与 --volume 必须同时指定")
    if sum([has_target, has_target_shares, has_exact_volume]) > 1:
        parser.error("--target / --target-shares / (--side + --volume) 三种数量模式互斥")
    if not any([has_target, has_target_shares, has_exact_volume]):
        args.target = 0.20
        has_target = True
    if has_target and not (0.0 <= args.target <= 1.0):
        parser.error("--target 必须在 0.0 到 1.0 之间")
    if has_target_shares and args.target_shares < 0:
        parser.error("--target-shares 不能为负数")
    if has_exact_volume and args.volume <= 0:
        parser.error("--volume 必须为正整数")

    quantity_mode = (
        "TARGET_WEIGHT" if has_target else
        "TARGET_SHARES" if has_target_shares else
        "EXACT_VOLUME"
    )

    source_settings = Path(args.settings).resolve()
    if not source_settings.exists():
        raise SystemExit(f"找不到配置文件: {source_settings}")

    temp_ctx = tempfile.TemporaryDirectory(prefix="positionx_order_preview_")
    temp_dir = Path(temp_ctx.name)
    keep_path = None
    service = None

    try:
        isolated_settings = _load_isolated_config(source_settings, temp_dir, args.symbol)
        service = TradingService(str(isolated_settings))

        if service.broker is None:
            raise RuntimeError("qmt.account_id 为空；请先在正式 config/settings.yaml 中配置 miniQMT 证券账户")

        # Second independent safety layer: even a future accidental code path
        # cannot submit or cancel through MiniQmtBroker in this test process.
        service.broker.submit_limit = _forbidden_action
        service.broker.cancel = _forbidden_action

        service.connect()
        account, positions, orders, trades = service.sync_broker()

        # Optional BLOCKED-path simulation. All overrides live only in this process
        # and/or the temporary SQLite DB. Production account state is never mutated.
        block_case = args.block_case
        test_overrides: Dict[str, Any] = {"block_case": block_case, "engine_path": args.engine_path, "quantity_mode": quantity_mode}

        if block_case == "kill-switch":
            service.risk.set_kill_switch(True)  # isolated DB only
            test_overrides["kill_switch"] = True

        elif block_case == "external-order":
            fake_external = [{
                "symbol": args.symbol,
                "source": "SIMULATED_EXTERNAL_ORDER",
                "note": "isolated test only",
            }]
            service.broker.active_external_orders = lambda: fake_external
            test_overrides["external_active_orders"] = 1

        elif block_case == "no-sellable":
            real_pos = next((p for p in positions if p.symbol == args.symbol), None)
            if real_pos is None:
                print(_json([{
                    "symbol": args.symbol,
                    "status": "TEST_SETUP_BLOCKED",
                    "block_type": "NO_REAL_POSITION",
                    "reason": "no-sellable 场景需要测试标的存在真实持仓；未修改任何账户状态",
                }]))
                return 0
            simulated_positions = [
                replace(p, can_use_volume=0) if p.symbol == args.symbol else p
                for p in positions
            ]
            service.broker.query_positions = lambda: simulated_positions
            test_overrides["can_use_volume_override"] = {
                "symbol": args.symbol,
                "real": int(real_pos.can_use_volume),
                "simulated": 0,
            }

        elif block_case == "no-cash":
            simulated_account = replace(account, cash=0.0)
            service.broker.query_account = lambda: simulated_account
            test_overrides["cash_override"] = {
                "real": float(account.cash),
                "simulated": 0.0,
            }

        stale_seconds = int(service.settings.get("market.stale_tick_seconds", 30))
        live_session = service.market.is_live_session(args.symbol, stale_seconds)
        tick = service.market.full_tick(args.symbol)
        tick_audit = _tick_audit(service.market, tick, stale_seconds)

        today = date.today().isoformat()

        if not live_session:
            position = next((p for p in positions if p.symbol == args.symbol), None)
            external_mv = sum(float(p.market_value or 0.0) for p in positions if p.symbol != args.symbol)
            external_weight = external_mv / account.total_asset if account.total_asset > 0 else 0.0
            bid1 = _first_number(tick.get("bidPrice") or tick.get("bid_price") or [])
            ask1 = _first_number(tick.get("askPrice") or tick.get("ask_price") or [])
            last = float(tick.get("lastPrice") or tick.get("last_price") or tick.get("price") or 0.0)

            diagnostics = {
                "mode": "ISOLATED_ORDER_PREVIEW",
                "engine_path": args.engine_path,
                "test_overrides": test_overrides,
                "safety": {
                    "production_settings": str(source_settings),
                    "production_db_opened": False,
                    "isolated_db": str(service.db.path),
                    "execution.live_enabled": service.settings.get("execution.live_enabled"),
                    "execution.auto_execute": service.settings.get("execution.auto_execute"),
                    "broker_submit_guard": "ENABLED",
                    "broker_cancel_guard": "ENABLED",
                },
                "session": {
                    "trade_date": today,
                    "symbol": args.symbol,
                    "session_open": False,
                    **tick_audit,
                    "openInt": tick.get("openInt"),
                    "stockStatus": tick.get("stockStatus"),
                    "last": last,
                    "bid1": bid1,
                    "ask1": ask1,
                },
                "account": {
                    "total_asset": account.total_asset,
                    "cash": account.cash,
                    "market_value": account.market_value,
                    "frozen_cash": account.frozen_cash,
                    "positions": len(positions),
                    "active_external_orders": len(service.broker.active_external_orders()),
                },
                "quantity_request": {
                    "quantity_mode": quantity_mode,
                    "target_shares": args.target_shares,
                    "side": args.side,
                    "volume": args.volume,
                },
                "risk_context": {
                    "planned_target_weight": float(args.target) if has_target else None,
                    "effective_target_weight": None,
                    "external_market_value": external_mv,
                    "external_weight": external_weight,
                    "max_total_weight": float(service.settings.get("risk.max_total_weight", 0.80)),
                    "max_symbol_weight": float(service.settings.get("risk.max_symbol_weight", 0.25)),
                    "min_cash_reserve": float(service.settings.get("risk.min_cash_reserve", 0.10)),
                    "max_order_pct_of_asset": float(service.settings.get("risk.max_order_pct_of_asset", 0.25)),
                    "rebalance_threshold_pct": float(service.settings.get("risk.rebalance_threshold_pct", 0.01)),
                    "min_order_value": float(service.settings.get("risk.min_order_value", 3000)),
                },
                "test_position": _position_row(position, account.total_asset),
            }
            preview = [{
                "trade_date": today,
                "symbol": args.symbol,
                "test_mode": "ISOLATED_ORDER_PREVIEW",
                "planned_target_weight": float(args.target) if has_target else None,
                "quantity_mode": quantity_mode,
                "target_shares": args.target_shares,
                "exact_volume": args.volume if has_exact_volume else None,
                "side": args.side,
                "status": "BLOCKED",
                "block_type": "SESSION_CLOSED_OR_STALE",
                "reason": f"当前不是已确认的连续交易时段，或行情超过 {stale_seconds}s；未进入订单计算",
            }]

            print("=== POSITIONX ISOLATED ORDER_PREVIEW ===")
            print(_json(diagnostics))
            print("\n=== PREVIEW RESULT ===")
            print(_json(preview))
            print("\nRESULT: SAFE_BLOCK - 交易时段/行情新鲜度检查阻止了订单预览；这是预期业务状态，不是程序异常。")

            if args.keep_temp:
                keep_path = Path.cwd() / "data" / "order_preview_test_artifact"
                if keep_path.exists():
                    shutil.rmtree(keep_path)
                shutil.copytree(temp_dir, keep_path)
                print(f"临时审计文件已保留到: {keep_path}")
            return 0

        # From here on diagnostics must reflect the same simulated broker view
        # that ExecutionEngine will read.
        execution_account = service.broker.query_account()
        execution_positions = service.broker.query_positions()

        engine = ExecutionEngine(
            service.db,
            service.broker,
            service.market,
            service.risk,
            service.settings.section("risk"),
            service.settings.section("execution"),
            service.settings.section("market"),
        )

        posmap = {p.symbol: p for p in execution_positions}
        position = posmap.get(args.symbol)
        effective_target = None

        if quantity_mode == "TARGET_WEIGHT":
            audit = {
                "test_mode": "ISOLATED_ORDER_PREVIEW",
                "signal_date": today,
                "note": "人工构造目标仓位，仅用于订单预览；非策略信号",
                "block_case": block_case,
                "quantity_mode": quantity_mode,
            }
            service.db.upsert_plan(
                TradingPlanItem(
                    plan_date=today,
                    trade_date=today,
                    symbol=args.symbol,
                    target_weight=float(args.target),
                    reason="ISOLATED ORDER_PREVIEW TEST ONLY",
                    strategy_name=TEST_STRATEGY_NAME,
                    audit=audit,
                )
            )
            plan = service.db.plan_for_date(today)
            effective_targets = engine._effective_targets(plan, execution_account, execution_positions)
            effective_target = float(effective_targets.get(args.symbol, 0.0) or 0.0)

        external_mv = sum(float(p.market_value or 0.0) for p in execution_positions if p.symbol != args.symbol)
        external_weight = external_mv / execution_account.total_asset if execution_account.total_asset > 0 else 0.0

        bid1 = _first_number(tick.get("bidPrice") or tick.get("bid_price") or [])
        ask1 = _first_number(tick.get("askPrice") or tick.get("ask_price") or [])
        last = float(tick.get("lastPrice") or tick.get("last_price") or tick.get("price") or 0.0)

        diagnostics = {
            "mode": "ISOLATED_ORDER_PREVIEW",
            "engine_path": args.engine_path,
            "test_overrides": test_overrides,
            "safety": {
                "production_settings": str(source_settings),
                "production_db_opened": False,
                "isolated_db": str(service.db.path),
                "execution.live_enabled": service.settings.get("execution.live_enabled"),
                "execution.auto_execute": service.settings.get("execution.auto_execute"),
                "broker_submit_guard": "ENABLED",
                "broker_cancel_guard": "ENABLED",
            },
            "session": {
                "trade_date": today,
                "symbol": args.symbol,
                "session_open": live_session,
                **tick_audit,
                "openInt": tick.get("openInt"),
                "stockStatus": tick.get("stockStatus"),
                "last": last,
                "bid1": bid1,
                "ask1": ask1,
            },
            "account": {
                "total_asset": execution_account.total_asset,
                "cash": execution_account.cash,
                "market_value": execution_account.market_value,
                "frozen_cash": execution_account.frozen_cash,
                "positions": len(execution_positions),
                "active_external_orders": len(service.broker.active_external_orders()),
            },
            "quantity_request": {
                "quantity_mode": quantity_mode,
                "target_shares": args.target_shares,
                "side": args.side,
                "volume": args.volume,
            },
            "risk_context": {
                "planned_target_weight": float(args.target) if has_target else None,
                "effective_target_weight": effective_target,
                "external_market_value": external_mv,
                "external_weight": external_weight,
                "max_total_weight": float(service.settings.get("risk.max_total_weight", 0.80)),
                "max_symbol_weight": float(service.settings.get("risk.max_symbol_weight", 0.25)),
                "min_cash_reserve": float(service.settings.get("risk.min_cash_reserve", 0.10)),
                "max_order_pct_of_asset": float(service.settings.get("risk.max_order_pct_of_asset", 0.25)),
                "rebalance_threshold_pct": float(service.settings.get("risk.rebalance_threshold_pct", 0.01)),
                "min_order_value": float(service.settings.get("risk.min_order_value", 3000)),
            },
            "test_position": _position_row(position, execution_account.total_asset),
        }

        # Exercise one of the exact production ExecutionEngine paths.
        # execute-guard deliberately uses dry_run=False, but the isolated config
        # hard-forces live_enabled=false and broker write methods are disabled.
        if quantity_mode == "TARGET_WEIGHT":
            preview = engine.execute_plan(today, dry_run=(args.engine_path == "dry-run"))
        else:
            preview = engine.execute_quantity_request(
                today,
                args.symbol,
                target_shares=args.target_shares if quantity_mode == "TARGET_SHARES" else None,
                side=args.side if quantity_mode == "EXACT_VOLUME" else None,
                volume=args.volume if quantity_mode == "EXACT_VOLUME" else None,
                dry_run=(args.engine_path == "dry-run"),
            )

        print("=== POSITIONX ISOLATED ORDER_PREVIEW ===")
        print(_json(diagnostics))
        print("\n=== PREVIEW RESULT ===")
        print(_json(preview))

        statuses = [str(x.get("status", "")) for x in preview]
        persisted_intents = [
            x.get("intent") for x in preview
            if x.get("intent") and service.db.has_intent(str(x.get("intent")))
        ]
        if "ORDER_PREVIEW" in statuses and args.engine_path == "execute-guard":
            if persisted_intents:
                print(f"\nRESULT: FAIL - live_enabled=false 的 execute 路径不应持久化intent: {persisted_intents}")
                return 2
            print("\nRESULT: PASS_EXECUTE_GUARD - 已走 dry_run=False 真实执行路径，但 live_enabled=false 阻止了提交；未持久化intent。")
        elif "ORDER_PREVIEW" in statuses:
            print("\nRESULT: PASS - 产生 ORDER_PREVIEW；没有提交任何委托。")
        elif "BLOCKED" in statuses:
            print(f"\nRESULT: PASS_BLOCK - BLOCKED路径生效（case={block_case}）；没有提交任何委托。")
        elif "NO_ACTION" in statuses:
            print("\nRESULT: NO_ACTION - 当前真实账户与风险约束下无需/无法形成有效调整。")
        else:
            print(f"\nRESULT: REVIEW - statuses={statuses}")

        if args.keep_temp:
            keep_path = Path.cwd() / "data" / "order_preview_test_artifact"
            if keep_path.exists():
                shutil.rmtree(keep_path)
            shutil.copytree(temp_dir, keep_path)
            print(f"临时审计文件已保留到: {keep_path}")

        return 0
    finally:
        if service is not None:
            try:
                service.disconnect()
            except Exception:
                pass
        temp_ctx.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
