from __future__ import annotations

from datetime import date
from pathlib import Path
import traceback

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QGroupBox, QPlainTextEdit,
    QStackedWidget, QButtonGroup
)

from core.service import TradingService
from core.scheduler import DailyScheduler
from .forward_page import ForwardPage
from .overview_page import OverviewPage
from .placeholder_page import PlaceholderPage
from .navigation_model import NAV_ITEMS, DEFAULT_PAGE


class Dashboard(QMainWindow):
    def __init__(self, config_path: str):
        super().__init__()
        self.service = TradingService(config_path)
        self.scheduler = DailyScheduler(self.service)
        self.setWindowTitle("PositionX Minimal Live")
        self.resize(1280, 820)
        self._build()
        self._apply_style()
        poll_ms = int(self.service.settings.get("schedule.poll_seconds", 5)) * 1000
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._heartbeat)
        self.timer.start(max(2000, poll_ms))
        self._refresh_events()
        self._refresh_view()

    def _build(self):
        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(180)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(14, 16, 14, 16)
        side.setSpacing(5)

        brand = QLabel("PositionX")
        brand.setStyleSheet("font-size:20px;font-weight:bold;color:#ffffff")
        side.addWidget(brand)
        sub = QLabel("Quant Operating System")
        sub.setStyleSheet("color:#7f8b99;font-size:10px")
        side.addWidget(sub)
        side.addSpacing(14)

        self.stack = QStackedWidget()
        self.pages = {}
        self.nav_buttons = {}
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)

        self.overview_page = OverviewPage(Path.cwd())
        self.forward_page = ForwardPage(Path.cwd())
        self.execution_page = self._build_execution_page()
        self.pages["dashboard"] = self.overview_page
        self.pages["forward"] = self.forward_page
        self.pages["execution"] = self.execution_page
        self.pages["strategies"] = PlaceholderPage(
            "Strategies",
            "v0.4：展示候选池、BreakoutRetest / MA60Support 等策略状态与证据。"
            "页面只消费已持久化研究结果，不在 GUI 内重新计算策略。",
        )
        self.pages["portfolio"] = PlaceholderPage(
            "Portfolio",
            "后续用于组合层：账户净值、敞口、策略风险预算与多策略资本分配。"
            "核心评价标准是净收益、回撤与资金效率。",
        )
        self.pages["system"] = PlaceholderPage(
            "System / Logs",
            "后续集中展示 QMT、数据、数据库、Forward journal、任务状态和日志。"
            "现阶段事件/告警仍完整保留在 Execution 页面。",
        )

        for key in ["dashboard", "strategies", "forward", "portfolio", "execution", "system"]:
            self.stack.addWidget(self.pages[key])

        last_group = None
        for item in NAV_ITEMS:
            if item["group"] != last_group:
                if last_group is not None:
                    side.addSpacing(8)
                group = QLabel(item["group"])
                group.setStyleSheet(
                    "color:#657180;font-size:9px;font-weight:bold;padding-top:4px"
                )
                side.addWidget(group)
                last_group = item["group"]

            b = QPushButton(item["label"])
            b.setCheckable(True)
            b.setEnabled(bool(item["enabled"]))
            b.setProperty("nav", True)
            b.clicked.connect(lambda checked=False, k=item["key"]: self._navigate(k))
            self.nav_group.addButton(b)
            self.nav_buttons[item["key"]] = b
            side.addWidget(b)

        side.addStretch()
        self.sidebar_status = QLabel("● STARTING")
        self.sidebar_status.setStyleSheet("font-weight:bold;color:#f0ad4e")
        side.addWidget(self.sidebar_status)
        self.sidebar_mode = QLabel("SAFE PREVIEW")
        self.sidebar_mode.setStyleSheet("color:#5bc0de")
        side.addWidget(self.sidebar_mode)

        root_layout.addWidget(sidebar)
        root_layout.addWidget(self.stack, 1)
        self.setCentralWidget(root)

        self._navigate(DEFAULT_PAGE)

    def _build_execution_page(self):
        execution = QWidget()
        layout = QVBoxLayout(execution)

        top = QHBoxLayout()
        self.status = QLabel("● NOT READY")
        self.status.setStyleSheet("font-weight:bold;color:#d9534f;font-size:16px")
        top.addWidget(self.status)
        self.mode = QLabel("SAFE PREVIEW"); top.addWidget(self.mode)
        self.conn = QLabel("QMT: 未连接"); top.addWidget(self.conn)
        self.account = QLabel("资产: -"); top.addWidget(self.account)
        top.addStretch()
        for text, fn in [
            ("连接/检查", self._check), ("生成下个交易时段计划", self._plan),
            ("Dry Run", self._dry_run), ("执行今日计划", self._execute),
            ("对账", self._reconcile), ("紧急停止", self._kill)
        ]:
            b = QPushButton(text); b.clicked.connect(fn); top.addWidget(b)
        layout.addLayout(top)

        self.positions = QTableWidget(0, 7)
        self.positions.setHorizontalHeaderLabels(
            ["代码","持仓","可用","成本","市值","当前权重","目标权重"]
        )
        self.positions.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        g1 = QGroupBox("实际持仓 / 今日目标")
        l1 = QVBoxLayout(g1); l1.addWidget(self.positions); layout.addWidget(g1)

        self._fast_ma = int(self.service.settings.get("strategy.params.fast_ma", 20))
        self._slow_ma = int(self.service.settings.get("strategy.params.slow_ma", 60))
        self.plan = QTableWidget(0, 9)
        self.plan.setHorizontalHeaderLabels([
            "交易日","信号日","代码","收盘",f"MA{self._fast_ma}",f"MA{self._slow_ma}",
            "目标仓位","状态","原因"
        ])
        self.plan.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        g2 = QGroupBox("交易计划")
        l2 = QVBoxLayout(g2); l2.addWidget(self.plan); layout.addWidget(g2)

        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setMaximumBlockCount(300)
        g3 = QGroupBox("事件 / 告警")
        l3 = QVBoxLayout(g3); l3.addWidget(self.events); layout.addWidget(g3)
        return execution

    def _navigate(self, key: str):
        page = self.pages.get(key)
        button = self.nav_buttons.get(key)
        if page is None or button is None or not button.isEnabled():
            return
        self.stack.setCurrentWidget(page)
        button.setChecked(True)
        if key == "forward":
            self.forward_page.refresh()
        elif key == "dashboard":
            self.overview_page.refresh_forward()

    def _apply_style(self):
        self.setStyleSheet("""
        QMainWindow,QWidget { background:#15171c; color:#e6e6e6; }
        QWidget#sidebar { background:#101217; border-right:1px solid #2d313a; }
        QGroupBox { border:1px solid #343842; margin-top:8px; padding-top:8px; font-weight:bold; }
        QPushButton { background:#2b303b; border:1px solid #464d5a; padding:6px 10px; border-radius:4px; }
        QPushButton:hover { background:#394150; }
        QPushButton[nav="true"] {
            text-align:left; background:transparent; border:none;
            padding:9px 10px; border-radius:4px; color:#c9d1db;
        }
        QPushButton[nav="true"]:hover { background:#202630; }
        QPushButton[nav="true"]:checked {
            background:#2d3747; color:#ffffff; font-weight:bold;
            border-left:3px solid #5bc0de;
        }
        QPushButton[nav="true"]:disabled { color:#505863; }
        QTableWidget,QPlainTextEdit { background:#101217; gridline-color:#2d313a; }
        QHeaderView::section { background:#222630; padding:5px; }
        QProgressBar { border:1px solid #343842; background:#101217; text-align:center; }
        QProgressBar::chunk { background:#394a5f; }
        """)

    def _safe(self, fn):
        try:
            return fn()
        except Exception as e:
            self.service.db.event("ERROR", "GUI", str(e), {"trace": traceback.format_exc()})
            QMessageBox.critical(self, "错误", str(e))
            return None
        finally:
            self._refresh_events(); self._refresh_view()

    def _check(self): self._safe(self.service.readiness_check)
    def _plan(self): self._safe(self.service.prepare_plan)
    def _dry_run(self):
        self._safe(lambda: self.service.execute_plan(date.today().isoformat(), dry_run=True))
    def _reconcile(self): self._safe(self.service.reconcile)

    def _execute(self):
        live_enabled = bool(self.service.settings.get("execution.live_enabled", False))
        pilot_mode = bool(self.service.settings.get("execution.pilot_mode", False))
        if not live_enabled:
            QMessageBox.warning(
                self, "实盘未武装",
                "execution.live_enabled=false。先用 Dry Run 验证；确认后再修改配置。"
            )
            return
        if pilot_mode:
            max_value = float(
                self.service.settings.get("execution.pilot_max_order_value", 0) or 0
            )
            max_orders = int(
                self.service.settings.get("execution.pilot_max_orders_per_day", 0) or 0
            )
            msg = (
                f"当前为 PILOT LIVE。\n单笔最大名义金额：{max_value:,.0f} 元\n"
                f"每日最多委托：{max_orders} 笔\n"
                "AUTO EXECUTE 在 Pilot 模式下被强制禁用。\n\n"
                "将人工提交今日计划，继续？"
            )
        else:
            msg = "当前为 FULL LIVE，将按今日交易计划向 miniQMT 提交真实委托，继续？"
        if QMessageBox.question(self, "确认实盘", msg) != QMessageBox.Yes:
            return
        self._safe(
            lambda: self.service.execute_plan(date.today().isoformat(), dry_run=False)
        )

    def _kill(self):
        on = not self.service.risk.kill_switch()
        self.service.risk.set_kill_switch(on)
        self.service.db.event("WARNING", "KILL_SWITCH", "ON" if on else "OFF")
        self._refresh_view()

    def _heartbeat(self):
        try:
            if self.service.broker and self.service.broker.connected:
                self.service.sync_broker()
            self.scheduler.tick()
        except Exception as e:
            self.service.db.event("ERROR", "HEARTBEAT", str(e))
        self._refresh_view()
        self._refresh_events()

    def _refresh_view(self):
        ready = (
            self.service.db.get_state("ready", "0") == "1"
            and not self.service.risk.kill_switch()
        )
        self.status.setText("● READY" if ready else "● NOT READY")
        self.status.setStyleSheet(
            f"font-weight:bold;font-size:16px;color:"
            f"{'#5cb85c' if ready else '#d9534f'}"
        )
        self.sidebar_status.setText("● READY" if ready else "● NOT READY")
        self.sidebar_status.setStyleSheet(
            f"font-weight:bold;color:{'#5cb85c' if ready else '#d9534f'}"
        )

        qmt_connected = bool(
            self.service.broker and self.service.broker.connected
        )
        self.conn.setText("QMT: 已连接" if qmt_connected else "QMT: 未连接")

        live_enabled = bool(
            self.service.settings.get("execution.live_enabled", False)
        )
        pilot_mode = bool(
            self.service.settings.get("execution.pilot_mode", False)
        )
        if not live_enabled:
            mode_text = "SAFE PREVIEW"
            mode_color = "#5bc0de"
        elif pilot_mode:
            mv = float(
                self.service.settings.get("execution.pilot_max_order_value", 0) or 0
            )
            mo = int(
                self.service.settings.get("execution.pilot_max_orders_per_day", 0) or 0
            )
            mode_text = f"PILOT LIVE ≤¥{mv:,.0f} / {mo}单/日"
            mode_color = "#f0ad4e"
        else:
            mode_text = "FULL LIVE"
            mode_color = "#d9534f"

        self.mode.setText(mode_text)
        self.mode.setStyleSheet(f"font-weight:bold;color:{mode_color}")
        self.sidebar_mode.setText(mode_text)
        self.sidebar_mode.setStyleSheet(f"color:{mode_color}")

        positions = []
        account = None
        if qmt_connected:
            try:
                account = self.service.broker.query_account()
                positions = self.service.broker.query_positions()
            except Exception:
                pass

        account_asset = account.total_asset if account else None
        self.account.setText(
            f"资产: {account_asset:,.2f}" if account_asset is not None else "资产: -"
        )

        today_plan = self.service.db.upcoming_plan(date.today().isoformat())
        target = {r["symbol"]: float(r["target_weight"]) for r in today_plan}
        symbols = sorted(set([p.symbol for p in positions]) | set(target))
        pmap = {p.symbol: p for p in positions}
        self.positions.setRowCount(len(symbols))

        for i, s in enumerate(symbols):
            p = pmap.get(s)
            tw = target.get(s, 0.0)
            cw = (
                p.market_value / account.total_asset
                if p and account and account.total_asset else 0.0
            )
            vals = [
                s,
                str(p.volume if p else 0),
                str(p.can_use_volume if p else 0),
                f"{p.avg_price:.2f}" if p else "-",
                f"{p.market_value:,.0f}" if p else "0",
                f"{cw:.1%}",
                f"{tw:.1%}",
            ]
            for j, v in enumerate(vals):
                self.positions.setItem(i, j, QTableWidgetItem(v))

        self.plan.setRowCount(len(today_plan))
        for i, r in enumerate(today_plan):
            audit = r.get("audit") or {}
            vals = [
                ("下个交易时段" if r["trade_date"] == "NEXT_SESSION" else r["trade_date"]),
                audit.get("signal_date", "-"),
                r["symbol"],
                f"{float(audit['close']):.2f}" if audit.get("close") is not None else "-",
                f"{float(audit[f'ma{self._fast_ma}']):.2f}"
                if audit.get(f"ma{self._fast_ma}") is not None else "-",
                f"{float(audit[f'ma{self._slow_ma}']):.2f}"
                if audit.get(f"ma{self._slow_ma}") is not None else "-",
                f"{r['target_weight']:.1%}",
                r["status"],
                r["reason"],
            ]
            for j, v in enumerate(vals):
                self.plan.setItem(i, j, QTableWidgetItem(str(v)))

        self.forward_page.refresh()
        self.overview_page.update_execution(
            ready=ready,
            mode=mode_text,
            qmt_connected=qmt_connected,
            account_asset=account_asset,
            plan_count=len(today_plan),
        )
        self.overview_page.refresh_forward()

    def _refresh_events(self):
        rows = self.service.db.recent_events(80)
        self.events.setPlainText(
            "\n".join(
                f"{r['ts']} [{r['level']}] {r['event_type']}: {r['message']}"
                for r in rows
            )
        )
        self.events.verticalScrollBar().setValue(
            self.events.verticalScrollBar().minimum()
        )

    def closeEvent(self, event):
        self.service.disconnect()
        event.accept()
