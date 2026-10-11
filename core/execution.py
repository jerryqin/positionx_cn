from __future__ import annotations

import hashlib
import math
from typing import Dict, Iterable, List

from .broker import MiniQmtBroker
from .data import MarketDataService
from .db import Database
from .models import AccountSnapshot, OrderIntent, PositionSnapshot, Side
from .risk import RiskGate


class ExecutionEngine:
    def __init__(self, db: Database, broker: MiniQmtBroker, market: MarketDataService, risk: RiskGate,
                 risk_cfg: Dict, execution_cfg: Dict, market_cfg: Dict):
        self.db = db
        self.broker = broker
        self.market = market
        self.risk = risk
        self.risk_cfg = risk_cfg
        self.cfg = execution_cfg
        self.market_cfg = market_cfg

    @staticmethod
    def _round_tick(price: float, tick: float) -> float:
        return round(round(price / tick) * tick, 2)

    def _pilot_limit(self, trade_date: str, intent: OrderIntent, risk_allowed_volume: int,
                     account: AccountSnapshot, additional_used: int = 0):
        """Apply the small-live pilot blast-radius guard after RiskGate.

        Pilot is deliberately an execution guard, not a strategy/risk model.
        It can only reduce/block an already-approved order; it can never enlarge it.
        """
        if not bool(self.cfg.get("pilot_mode", False)):
            return int(risk_allowed_volume), None, {"pilot_mode": False}

        max_orders = max(0, int(self.cfg.get("pilot_max_orders_per_day", 1) or 0))
        max_value = max(0.0, float(self.cfg.get("pilot_max_order_value", 0) or 0))
        used = self.db.count_order_intents_for_date(trade_date) + max(0, int(additional_used))
        meta = {
            "pilot_mode": True,
            "pilot_orders_used": used,
            "pilot_max_orders_per_day": max_orders,
            "pilot_max_order_value": max_value,
        }
        if max_orders <= 0 or used >= max_orders:
            return 0, f"Pilot每日委托上限已达到：{used}/{max_orders}", meta
        if max_value <= 0 or intent.limit_price <= 0:
            return 0, "Pilot单笔金额上限未正确配置", meta

        cap_volume = int(max_value // intent.limit_price)
        if intent.side == Side.BUY:
            default_lot = int(self.risk_cfg.get("buy_lot", 100))
            lot = self.market.min_limit_order_volume(intent.symbol, default_lot)
            cap_volume = (cap_volume // lot) * lot
        final_volume = min(int(risk_allowed_volume), cap_volume)
        if final_volume <= 0:
            return 0, f"Pilot单笔金额上限 {max_value:.0f} 元不足以形成有效委托", meta

        min_order_value = float(self.risk_cfg.get("min_order_value", 3000))
        if final_volume * intent.limit_price < min_order_value:
            return 0, f"Pilot限额后委托金额低于最小委托金额 {min_order_value:.0f} 元", meta
        meta["pilot_limited"] = final_volume < int(risk_allowed_volume)
        return final_volume, None, meta

    def _limit_price(self, symbol: str, side: Side) -> float:
        self.market.ensure_tradable(symbol)
        tick = self.market.full_tick(symbol)
        if not self.market.is_tick_fresh(tick, int(self.market_cfg.get("stale_tick_seconds", 30))):
            raise RuntimeError(f"{symbol} 行情快照过期")
        # 股票连续交易状态看 openInt/openInterest。stockStatus 使用另一套
        # 编码，真实 miniQMT 快照中连续交易常见 stockStatus=3、openInt=13。
        state = MarketDataService.security_state(tick)
        if state != 13:
            raise RuntimeError(f"{symbol} 当前证券状态(openInt)={state}，非连续交易状态，拒绝下单")
        base = self.market.tick_price(tick, side.value)
        bps = float(self.cfg.get("limit_offset_bps", 20)) / 10000.0
        px = base * (1 + bps) if side == Side.BUY else base * (1 - bps)
        down, up = self.market.price_limits(symbol)
        if up > 0: px = min(px, up)
        if down > 0: px = max(px, down)
        tick_size = self.market.price_tick(symbol, float(self.market_cfg.get("default_price_tick", 0.01)))
        return self._round_tick(px, tick_size)

    def _effective_targets(self, plan_rows: List[Dict], account: AccountSnapshot,
                           positions: List[PositionSnapshot]) -> Dict[str, float]:
        """Apply target caps and external-holding capacity exactly once.

        This is shared by order construction and dry-run audit so the preview
        always explains the same effective target that execution would use.
        """
        raw_targets = {r["symbol"]: float(r["target_weight"]) for r in plan_rows}
        targets = self.risk.validate_target_set(raw_targets)
        plan_symbols = set(raw_targets)
        external_mv = sum(float(p.market_value) for p in positions if p.symbol not in plan_symbols)
        external_weight = external_mv / account.total_asset if account.total_asset > 0 else 0.0
        max_total = float(self.risk_cfg.get("max_total_weight", 0.80))
        available_for_plan = max(0.0, max_total - external_weight)
        target_sum = sum(targets.values())
        if target_sum > available_for_plan and target_sum > 0:
            scale = available_for_plan / target_sum
            targets = {s: w * scale for s, w in targets.items()}
        return targets

    def build_intents(self, trade_date: str, plan_rows: List[Dict], account: AccountSnapshot,
                      positions: List[PositionSnapshot]) -> List[OrderIntent]:
        pos = {p.symbol: p for p in positions}
        targets = self._effective_targets(plan_rows, account, positions)
        threshold = float(self.risk_cfg.get("rebalance_threshold_pct", 0.01))
        min_order_value = float(self.risk_cfg.get("min_order_value", 3000))
        lot = int(self.risk_cfg.get("buy_lot", 100))
        intents: List[OrderIntent] = []

        for row in plan_rows:
            symbol = row["symbol"]
            target = targets.get(symbol, 0.0)
            p = pos.get(symbol)
            current_mv = float(p.market_value if p else 0)
            current_weight = current_mv / account.total_asset if account.total_asset > 0 else 0.0
            if abs(target - current_weight) <= threshold:
                continue
            side = Side.BUY if target > current_weight else Side.SELL
            price = self._limit_price(symbol, side)
            target_value = account.total_asset * target
            delta_value = target_value - current_mv
            raw_volume = int(abs(delta_value) // price)
            if side == Side.BUY:
                symbol_lot = self.market.min_limit_order_volume(symbol, lot)
                raw_volume = (raw_volume // symbol_lot) * symbol_lot
            else:
                # SELL请求数量绝不应超过真实持仓。特别是目标=0时，
                # 直接以持仓股数作为理论退出数量，避免“持仓市值 / 偏移后限价”
                # 反推出比真实持仓多1股的审计噪声。最终可卖上限仍由RiskGate
                # 用 can_use_volume 再做一次硬约束。
                held_volume = int(p.volume if p else 0)
                if target <= 1e-12:
                    raw_volume = held_volume
                else:
                    raw_volume = min(raw_volume, held_volume)
            if raw_volume <= 0 or raw_volume * price < min_order_value:
                continue
            base = f"{trade_date}|{symbol}|{side.value}|{target:.6f}"
            key = hashlib.sha256(base.encode()).hexdigest()[:24]
            intents.append(OrderIntent(key,trade_date,symbol,side,raw_volume,price,row["reason"],target,current_weight))
        return intents

    def _dry_run_preview(self, trade_date: str, plan: List[Dict], account: AccountSnapshot,
                         positions: List[PositionSnapshot]) -> List[Dict]:
        """Return one auditable result per plan row without creating intents/orders."""
        posmap = {p.symbol: p for p in positions}
        targets = self._effective_targets(plan, account, positions)
        intents = self.build_intents(trade_date, plan, account, positions)
        intent_by_symbol = {i.symbol: i for i in intents}
        threshold = float(self.risk_cfg.get("rebalance_threshold_pct", 0.01))
        min_order_value = float(self.risk_cfg.get("min_order_value", 3000))
        results: List[Dict] = []
        pilot_preview_reserved = 0

        for row in plan:
            symbol = row["symbol"]
            audit = dict(row.get("audit") or {})
            p = posmap.get(symbol)
            current_mv = float(p.market_value if p else 0.0)
            current_weight = current_mv / account.total_asset if account.total_asset > 0 else 0.0
            planned_target = float(row.get("target_weight", 0.0) or 0.0)
            target = float(targets.get(symbol, 0.0) or 0.0)
            difference = target - current_weight
            base = {
                "trade_date": trade_date,
                "symbol": symbol,
                **audit,
                "plan_status": row.get("status", ""),
                "planned_target_weight": planned_target,
                "effective_target_weight": target,
                # target_weight保留为兼容字段；其语义始终等于effective_target_weight。
                "target_weight": target,
                "current_weight": current_weight,
                "difference": difference,
            }
            intent = intent_by_symbol.get(symbol)
            if intent is None:
                if abs(difference) <= threshold:
                    base.update({
                        "status": "NO_ACTION",
                        "reason": f"目标仓位已达到：|差异|={abs(difference):.2%} <= 再平衡阈值 {threshold:.2%}",
                    })
                else:
                    base.update({
                        "status": "NO_ACTION",
                        "reason": f"调整规模低于最小委托条件（最小委托金额 {min_order_value:.0f} 元或整数手约束）",
                    })
                results.append(base)
                continue

            if self.db.has_intent(intent.intent_key):
                base.update({
                    "status": "BLOCKED",
                    "intent": intent.intent_key,
                    "side": intent.side.value,
                    "requested_volume": intent.volume,
                    "allowed_volume": None,
                    "volume": 0,
                    "price": intent.limit_price,
                    "reason": "相同交易意图已存在，防重复下单",
                })
                results.append(base)
                continue

            decision = self.risk.check_intent(intent, account, posmap.get(symbol))
            if not decision.allowed:
                base.update({
                    "status": "BLOCKED",
                    "intent": intent.intent_key,
                    "side": intent.side.value,
                    "requested_volume": intent.volume,
                    "allowed_volume": int(decision.volume),
                    "volume": 0,
                    "price": intent.limit_price,
                    "reason": decision.message,
                })
                results.append(base)
                continue

            risk_allowed_volume = int(decision.volume)
            preview_volume, pilot_block, pilot_meta = self._pilot_limit(
                trade_date, intent, risk_allowed_volume, account, additional_used=pilot_preview_reserved
            )
            if pilot_block:
                base.update({
                    "status": "BLOCKED",
                    "intent": intent.intent_key,
                    "side": intent.side.value,
                    "requested_volume": intent.volume,
                    "risk_allowed_volume": risk_allowed_volume,
                    "allowed_volume": 0,
                    "volume": 0,
                    "price": intent.limit_price,
                    **pilot_meta,
                    "reason": pilot_block,
                })
                results.append(base)
                continue
            if pilot_meta.get("pilot_mode"):
                pilot_preview_reserved += 1
            reason = "RiskGate PASS；仅预览，不提交委托"
            if pilot_meta.get("pilot_limited"):
                reason = "RiskGate PASS；Pilot单笔金额限制已缩量；仅预览，不提交委托"
            base.update({
                "status": "ORDER_PREVIEW",
                "intent": intent.intent_key,
                "side": intent.side.value,
                "requested_volume": intent.volume,
                "risk_allowed_volume": risk_allowed_volume,
                "allowed_volume": preview_volume,
                "volume": preview_volume,
                "price": intent.limit_price,
                "notional": round(preview_volume * intent.limit_price, 2),
                **pilot_meta,
                "reason": reason,
            })
            results.append(base)
        return results


    def execute_quantity_request(
        self,
        trade_date: str,
        symbol: str,
        *,
        target_shares: int | None = None,
        side: str | Side | None = None,
        volume: int | None = None,
        dry_run: bool = True,
    ) -> List[Dict]:
        """Execute/preview an explicit share-based request through the normal guards.

        This path is deliberately separate from Strategy target_weight execution.

        Supported modes:
        - TARGET_SHARES: final desired broker holding, e.g. 3000 -> 2100 means SELL 900.
        - EXACT_VOLUME: explicit one-shot BUY/SELL volume.

        Exact semantics are fail-closed: RiskGate/Pilot may block an exact request,
        but may never silently shrink it to another order size.
        """
        has_target_shares = target_shares is not None
        has_exact_volume = side is not None or volume is not None
        if has_target_shares == has_exact_volume:
            raise ValueError("必须且只能指定 target_shares 或 side+volume 其中一种")
        if has_exact_volume and (side is None or volume is None):
            raise ValueError("EXACT_VOLUME 模式必须同时指定 side 和 volume")

        if bool(self.risk_cfg.get("stop_on_external_active_order", True)):
            external = self.broker.active_external_orders()
            if external:
                return [{
                    "trade_date": trade_date,
                    "symbol": symbol,
                    "status": "BLOCKED",
                    "block_type": "EXTERNAL_ACTIVE_ORDER",
                    "quantity_mode": "TARGET_SHARES" if has_target_shares else "EXACT_VOLUME",
                    "reason": f"发现非 PositionX 活跃委托 {len(external)} 笔，拒绝精确数量订单",
                }]

        account = self.broker.query_account()
        positions = self.broker.query_positions()
        posmap = {p.symbol: p for p in positions}
        position = posmap.get(symbol)
        current_shares = int(position.volume if position else 0)
        current_mv = float(position.market_value if position else 0.0)
        current_weight = current_mv / account.total_asset if account.total_asset > 0 else 0.0

        if has_target_shares:
            mode = "TARGET_SHARES"
            final_target_shares = int(target_shares)
            if final_target_shares < 0:
                raise ValueError("target_shares 不能为负数")
            delta = final_target_shares - current_shares
            if delta == 0:
                return [{
                    "trade_date": trade_date,
                    "symbol": symbol,
                    "status": "NO_ACTION",
                    "quantity_mode": mode,
                    "current_shares": current_shares,
                    "target_shares": final_target_shares,
                    "requested_volume": 0,
                    "allowed_volume": 0,
                    "volume": 0,
                    "reason": "真实持仓已达到 target_shares",
                }]
            order_side = Side.BUY if delta > 0 else Side.SELL
            requested_volume = abs(int(delta))
            key_material = f"{trade_date}|{symbol}|TARGET_SHARES|{final_target_shares}"
            reason = f"MANUAL TARGET_SHARES target={final_target_shares}"
            exact_volume_value = None
        else:
            mode = "EXACT_VOLUME"
            order_side = side if isinstance(side, Side) else Side(str(side).upper())
            requested_volume = int(volume)
            if requested_volume <= 0:
                raise ValueError("volume 必须为正整数")
            final_target_shares = (
                current_shares + requested_volume
                if order_side == Side.BUY
                else current_shares - requested_volume
            )
            key_material = f"{trade_date}|{symbol}|EXACT_VOLUME|{order_side.value}|{requested_volume}"
            reason = f"MANUAL EXACT_VOLUME {order_side.value} {requested_volume}"
            exact_volume_value = requested_volume

        price = self._limit_price(symbol, order_side)
        projected_mv = (
            current_mv + requested_volume * price
            if order_side == Side.BUY
            else max(0.0, current_mv - requested_volume * price)
        )
        target_weight_compat = (
            projected_mv / account.total_asset if account.total_asset > 0 else current_weight
        )
        intent_key = hashlib.sha256(key_material.encode()).hexdigest()[:24]
        intent = OrderIntent(
            intent_key,
            trade_date,
            symbol,
            order_side,
            requested_volume,
            price,
            reason,
            target_weight_compat,
            current_weight,
        )

        base = {
            "trade_date": trade_date,
            "symbol": symbol,
            "quantity_mode": mode,
            "current_shares": current_shares,
            "target_shares": final_target_shares,
            "exact_volume": exact_volume_value,
            "side": order_side.value,
            "requested_volume": requested_volume,
            "price": price,
            "current_weight": current_weight,
            "target_weight_compat": target_weight_compat,
            "intent": intent_key,
        }

        min_order_value = float(self.risk_cfg.get("min_order_value", 3000))
        if requested_volume * price < min_order_value:
            return [{
                **base,
                "status": "BLOCKED",
                "block_type": "MIN_ORDER_VALUE",
                "risk_allowed_volume": 0,
                "allowed_volume": 0,
                "volume": 0,
                "reason": f"精确数量订单金额低于最小委托金额 {min_order_value:.0f} 元",
            }]

        # Strategy target_weight validation normally enforces symbol/portfolio
        # caps before build_intents().  Exact BUY bypasses that layer, so apply
        # the equivalent hard exposure gates here.  SELL only reduces exposure.
        if order_side == Side.BUY and account.total_asset > 0:
            external_mv = sum(
                float(p.market_value or 0.0)
                for p in positions
                if p.symbol != symbol
            )
            symbol_weight_after = projected_mv / account.total_asset
            total_weight_after = (external_mv + projected_mv) / account.total_asset
            max_symbol = float(self.risk_cfg.get("max_symbol_weight", 0.25))
            max_total = float(self.risk_cfg.get("max_total_weight", 0.80))
            if symbol_weight_after > max_symbol + 1e-12:
                return [{
                    **base,
                    "status": "BLOCKED",
                    "block_type": "MAX_SYMBOL_WEIGHT",
                    "risk_allowed_volume": 0,
                    "allowed_volume": 0,
                    "volume": 0,
                    "symbol_weight_after": symbol_weight_after,
                    "max_symbol_weight": max_symbol,
                    "reason": "精确数量 BUY 会突破单标的最大仓位；拒绝自动缩量",
                }]
            if total_weight_after > max_total + 1e-12:
                return [{
                    **base,
                    "status": "BLOCKED",
                    "block_type": "MAX_TOTAL_WEIGHT",
                    "risk_allowed_volume": 0,
                    "allowed_volume": 0,
                    "volume": 0,
                    "total_weight_after": total_weight_after,
                    "max_total_weight": max_total,
                    "reason": "精确数量 BUY 会突破组合总仓位上限；拒绝自动缩量",
                }]

        if self.db.has_intent(intent.intent_key):
            return [{
                **base,
                "status": "SKIP_DUPLICATE",
                "allowed_volume": None,
                "volume": 0,
                "reason": "相同精确数量交易意图已存在，防重复下单",
            }]

        decision = self.risk.check_intent(intent, account, position)
        risk_allowed_volume = int(decision.volume)
        if not decision.allowed:
            return [{
                **base,
                "status": "BLOCKED",
                "block_type": "RISK_BLOCK",
                "risk_allowed_volume": risk_allowed_volume,
                "allowed_volume": 0,
                "volume": 0,
                "reason": decision.message,
            }]

        # Exact means exact.  RiskGate may reduce ordinary target_weight orders,
        # but share-based manual/Pilot requests are a contract: either the whole
        # requested quantity is allowed or nothing is submitted.
        if risk_allowed_volume != requested_volume:
            return [{
                **base,
                "status": "BLOCKED_EXACT_VOLUME",
                "block_type": "RISK_WOULD_RESIZE",
                "risk_allowed_volume": risk_allowed_volume,
                "allowed_volume": 0,
                "volume": 0,
                "reason": (
                    f"精确数量模式要求 {requested_volume} 股；"
                    f"RiskGate 仅允许 {risk_allowed_volume} 股，禁止自动缩量"
                ),
            }]

        pilot_allowed_volume, pilot_block, pilot_meta = self._pilot_limit(
            trade_date, intent, risk_allowed_volume, account
        )
        if pilot_block:
            return [{
                **base,
                "status": "BLOCKED",
                "block_type": "PILOT_BLOCK",
                "risk_allowed_volume": risk_allowed_volume,
                "allowed_volume": 0,
                "volume": 0,
                **pilot_meta,
                "reason": pilot_block,
            }]
        if int(pilot_allowed_volume) != requested_volume:
            return [{
                **base,
                "status": "BLOCKED_EXACT_VOLUME",
                "block_type": "PILOT_WOULD_RESIZE",
                "risk_allowed_volume": risk_allowed_volume,
                "pilot_allowed_volume": int(pilot_allowed_volume),
                "allowed_volume": 0,
                "volume": 0,
                **pilot_meta,
                "reason": (
                    f"精确数量模式要求 {requested_volume} 股；"
                    f"Pilot 限额仅允许 {int(pilot_allowed_volume)} 股，禁止自动缩量"
                ),
            }]

        live_enabled = bool(self.cfg.get("live_enabled", False))
        if dry_run or not live_enabled:
            return [{
                **base,
                "status": "ORDER_PREVIEW",
                "risk_allowed_volume": risk_allowed_volume,
                "allowed_volume": requested_volume,
                "volume": requested_volume,
                "notional": round(requested_volume * price, 2),
                **pilot_meta,
                "reason": (
                    "精确数量校验 PASS；仅预览，不提交委托"
                    if dry_run else
                    "精确数量校验 PASS；execution.live_enabled=false；未提交委托"
                ),
            }]

        self.db.save_intent(intent)
        remark = f"PX|{trade_date.replace('-','')}|{intent.intent_key[:8]}"
        try:
            order_id = self.broker.submit_limit(
                intent.symbol,
                intent.side.value,
                intent.volume,
                intent.limit_price,
                remark,
            )
            self.db.update_intent_order(intent.intent_key, "SUBMITTED", order_id)
            return [{
                **base,
                "status": "SUBMITTED",
                "order_id": order_id,
                "risk_allowed_volume": risk_allowed_volume,
                "allowed_volume": requested_volume,
                "volume": requested_volume,
                "notional": round(requested_volume * price, 2),
                **pilot_meta,
            }]
        except Exception as e:
            self.db.update_intent_order(intent.intent_key, "ERROR")
            self.db.event("ERROR", "ORDER_ERROR", str(e), {"intent_key": intent.intent_key})
            return [{
                **base,
                "status": "ERROR",
                "allowed_volume": 0,
                "volume": 0,
                "message": str(e),
            }]

    def execute_plan(self, trade_date: str, dry_run: bool = False) -> List[Dict]:
        plan = self.db.plan_for_date(trade_date)
        if not plan:
            if dry_run:
                return [{
                    "trade_date": trade_date,
                    "status": "NO_PLAN",
                    "reason": "今日没有可执行交易计划",
                }]
            return []
        if not self.broker.connected:
            raise RuntimeError("Broker 未连接")
        if bool(self.risk_cfg.get("stop_on_external_active_order", True)):
            external = self.broker.active_external_orders()
            if external:
                if dry_run:
                    return [{
                        "trade_date": trade_date,
                        "symbol": row.get("symbol", ""),
                        "status": "BLOCKED",
                        "plan_status": row.get("status", ""),
                        "planned_target_weight": float(row.get("target_weight", 0.0) or 0.0),
                        # 提前阻断发生在账户/持仓读取和有效目标计算之前，
                        # 所以这里不能把原始计划目标伪装成effective target。
                        "effective_target_weight": None,
                        "target_weight": None,
                        "current_weight": None,
                        "difference": None,
                        "block_type": "EXTERNAL_ACTIVE_ORDER",
                        "reason": f"发现非 PositionX 活跃委托 {len(external)} 笔，停止自动执行",
                    } for row in plan]
                raise RuntimeError(f"发现非 PositionX 活跃委托 {len(external)} 笔，停止自动执行")

        account = self.broker.query_account()
        positions = self.broker.query_positions()
        if dry_run:
            return self._dry_run_preview(trade_date, plan, account, positions)

        posmap = {p.symbol: p for p in positions}
        intents = self.build_intents(trade_date, plan, account, positions)
        results = []
        live_enabled = bool(self.cfg.get("live_enabled", False))

        for intent in intents:
            requested_volume = int(intent.volume)
            if self.db.has_intent(intent.intent_key):
                results.append({
                    "intent": intent.intent_key, "status": "SKIP_DUPLICATE",
                    "symbol": intent.symbol, "requested_volume": requested_volume,
                    "allowed_volume": None, "volume": 0,
                })
                continue
            decision = self.risk.check_intent(intent, account, posmap.get(intent.symbol))
            if not decision.allowed:
                results.append({
                    "intent": intent.intent_key, "status": "BLOCKED",
                    "message": decision.message, "symbol": intent.symbol,
                    "requested_volume": requested_volume,
                    "allowed_volume": int(decision.volume), "volume": 0,
                })
                self.db.event("WARNING", "RISK_BLOCK", decision.message, {"intent_key": intent.intent_key})
                continue
            risk_allowed_volume = int(decision.volume)
            allowed_volume, pilot_block, pilot_meta = self._pilot_limit(
                trade_date, intent, risk_allowed_volume, account
            )
            if pilot_block:
                results.append({
                    "intent": intent.intent_key, "status": "BLOCKED",
                    "message": pilot_block, "reason": pilot_block, "symbol": intent.symbol,
                    "side": intent.side.value, "requested_volume": requested_volume,
                    "risk_allowed_volume": risk_allowed_volume,
                    "allowed_volume": 0, "volume": 0, "price": intent.limit_price,
                    **pilot_meta,
                })
                self.db.event("WARNING", "PILOT_BLOCK", pilot_block, {"intent_key": intent.intent_key})
                continue
            if allowed_volume != intent.volume:
                intent = OrderIntent(intent.intent_key,intent.trade_date,intent.symbol,intent.side,allowed_volume,
                                     intent.limit_price,intent.reason,intent.target_weight,intent.current_weight)
            if not live_enabled:
                # live_enabled=false must remain a hard no-submit guard even if
                # somebody calls the real execution command accidentally.
                results.append({
                    "intent": intent.intent_key,
                    "status": "ORDER_PREVIEW",
                    "symbol": intent.symbol,
                    "side": intent.side.value,
                    "requested_volume": requested_volume,
                    "risk_allowed_volume": risk_allowed_volume,
                    "allowed_volume": allowed_volume,
                    "volume": intent.volume,
                    "price": intent.limit_price,
                    "notional": round(intent.volume * intent.limit_price, 2),
                    "target_weight": intent.target_weight,
                    "current_weight": intent.current_weight,
                    **pilot_meta,
                    "reason": (
                        "execution.live_enabled=false；Pilot单笔金额限制已缩量；未提交委托"
                        if pilot_meta.get("pilot_limited") else
                        "execution.live_enabled=false；未提交委托"
                    ),
                })
                continue
            self.db.save_intent(intent)
            remark = f"PX|{trade_date.replace('-','')}|{intent.intent_key[:8]}"
            try:
                order_id = self.broker.submit_limit(intent.symbol, intent.side.value, intent.volume, intent.limit_price, remark)
                self.db.update_intent_order(intent.intent_key, "SUBMITTED", order_id)
                results.append({
                    "intent": intent.intent_key, "status": "SUBMITTED", "order_id": order_id,
                    "symbol": intent.symbol, "side": intent.side.value,
                    "requested_volume": requested_volume, "risk_allowed_volume": risk_allowed_volume,
                    "allowed_volume": allowed_volume, "volume": intent.volume,
                    "price": intent.limit_price, "notional": round(intent.volume * intent.limit_price, 2),
                    **pilot_meta,
                })
            except Exception as e:
                self.db.update_intent_order(intent.intent_key, "ERROR")
                self.db.event("ERROR", "ORDER_ERROR", str(e), {"intent_key": intent.intent_key})
                results.append({"intent": intent.intent_key, "status": "ERROR", "message": str(e), "symbol": intent.symbol})
        return results
