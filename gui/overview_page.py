from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox, QGridLayout
)

from .forward_reader import load_forward_view


def _pct(v: Any, digits: int = 2) -> str:
    try:
        return f"{float(v):.{digits}%}"
    except Exception:
        return "-"


def _money(v: Any) -> str:
    try:
        return f"¥{float(v):,.2f}"
    except Exception:
        return "-"


class OverviewPage(QWidget):
    """
    Profit-first overview.

    This page is presentation-only. Execution state is pushed in from the
    existing Dashboard service flow; Forward state is read from persisted JSON.
    """
    def __init__(self, project_root: Path | str = ".", parent=None):
        super().__init__(parent)
        self.project_root = Path(project_root)
        self._execution_snapshot = {}
        self._build()
        self.refresh_forward()

    def _metric(self, title: str):
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 4, 8, 4)
        cap = QLabel(title)
        cap.setStyleSheet("color:#9aa4b2;font-size:11px")
        val = QLabel("-")
        val.setStyleSheet("font-size:19px;font-weight:bold")
        layout.addWidget(cap)
        layout.addWidget(val)
        return box, val

    def _build(self):
        root = QVBoxLayout(self)

        title_row = QHBoxLayout()
        title = QLabel("PositionX Dashboard")
        title.setStyleSheet("font-size:22px;font-weight:bold")
        title_row.addWidget(title)
        title_row.addStretch()
        principle = QLabel("目标：净收益增长，而不是为了研究而研究")
        principle.setStyleSheet("color:#5bc0de;font-weight:bold")
        title_row.addWidget(principle)
        root.addLayout(title_row)

        money = QGroupBox("Economic Scoreboard")
        mg = QGridLayout(money)
        metrics = [
            ("account_asset", "实盘账户资产"),
            ("forward_equity", "Forward 模拟权益"),
            ("forward_return", "Forward 累计收益"),
            ("forward_mdd", "Forward 最大回撤"),
            ("forward_avg", "20bp 平均单笔"),
            ("forward_pf", "20bp Profit Factor"),
        ]
        self.money_labels = {}
        for i, (key, title_text) in enumerate(metrics):
            box, label = self._metric(title_text)
            self.money_labels[key] = label
            mg.addWidget(box, i // 3, i % 3)
        root.addWidget(money)

        system = QGroupBox("System / Execution")
        sg = QGridLayout(system)
        sys_metrics = [
            ("ready", "系统状态"),
            ("mode", "执行模式"),
            ("qmt", "QMT"),
            ("plan_count", "当前计划"),
        ]
        self.system_labels = {}
        for i, (key, title_text) in enumerate(sys_metrics):
            box, label = self._metric(title_text)
            self.system_labels[key] = label
            sg.addWidget(box, 0, i)
        root.addWidget(system)

        forward = QGroupBox("BreakoutRetest Forward")
        fg = QGridLayout(forward)
        fw_metrics = [
            ("status", "状态"),
            ("progress", "完成交易"),
            ("active", "当前持仓"),
            ("pending", "Pending"),
            ("review", "最早复审"),
            ("shadow_pf", "50bp Shadow PF"),
        ]
        self.forward_labels = {}
        for i, (key, title_text) in enumerate(fw_metrics):
            box, label = self._metric(title_text)
            self.forward_labels[key] = label
            fg.addWidget(box, i // 3, i % 3)
        root.addWidget(forward)

        note = QGroupBox("当前阶段")
        nl = QVBoxLayout(note)
        self.stage_note = QLabel(
            "BreakoutRetest 已冻结参数并进入真实 Forward Simulation。"
            "在达到 ≥60 completed trades 且 ≥3个月之前，不因短期盈亏修改策略。"
        )
        self.stage_note.setWordWrap(True)
        self.stage_note.setStyleSheet("color:#b9c1cc")
        nl.addWidget(self.stage_note)
        root.addWidget(note)
        root.addStretch()

    def update_execution(
        self,
        *,
        ready: bool,
        mode: str,
        qmt_connected: bool,
        account_asset: Any,
        plan_count: int,
    ):
        self._execution_snapshot = {
            "ready": ready,
            "mode": mode,
            "qmt_connected": qmt_connected,
            "account_asset": account_asset,
            "plan_count": plan_count,
        }
        self.system_labels["ready"].setText("READY" if ready else "NOT READY")
        self.system_labels["ready"].setStyleSheet(
            f"font-size:19px;font-weight:bold;color:{'#5cb85c' if ready else '#d9534f'}"
        )
        self.system_labels["mode"].setText(mode or "-")
        self.system_labels["qmt"].setText("Connected" if qmt_connected else "Disconnected")
        self.system_labels["plan_count"].setText(str(plan_count))
        self.money_labels["account_asset"].setText(_money(account_asset))

    def refresh_forward(self):
        view = load_forward_view(self.project_root)
        if not view.get("available"):
            self.money_labels["forward_equity"].setText("-")
            self.money_labels["forward_return"].setText("-")
            self.money_labels["forward_mdd"].setText("-")
            self.money_labels["forward_avg"].setText("-")
            self.money_labels["forward_pf"].setText("-")
            for key in self.forward_labels:
                self.forward_labels[key].setText("-")
            self.forward_labels["status"].setText(view.get("status", "NOT_INITIALIZED"))
            return

        base = view.get("base_20bps") or {}
        shadow = view.get("shadow_50bps") or {}
        self.money_labels["forward_equity"].setText(_money(view.get("equity")))
        self.money_labels["forward_return"].setText(_pct(view.get("total_return")))
        self.money_labels["forward_mdd"].setText(_pct(view.get("max_drawdown")))
        self.money_labels["forward_avg"].setText(_pct(base.get("avg")))
        pf = base.get("profit_factor")
        self.money_labels["forward_pf"].setText(f"{float(pf):.3f}" if pf is not None else "-")

        done = int(view.get("completed_trades", 0))
        target = int(view.get("completed_trades_target", 60))
        self.forward_labels["status"].setText(str(view.get("status") or "-"))
        self.forward_labels["progress"].setText(f"{done} / {target}")
        self.forward_labels["active"].setText(str(view.get("active_positions", 0)))
        self.forward_labels["pending"].setText(str(view.get("pending_signals", 0)))
        self.forward_labels["review"].setText(str(view.get("review_not_before") or "-"))
        spf = shadow.get("profit_factor")
        self.forward_labels["shadow_pf"].setText(
            f"{float(spf):.3f}" if spf is not None else "-"
        )
