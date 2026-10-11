from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGroupBox,
    QGridLayout, QProgressBar, QTableWidget, QTableWidgetItem, QHeaderView,
    QTabWidget
)

from .forward_reader import load_forward_view


def _fmt_pct(v: Any, digits: int = 2) -> str:
    try:
        return f"{float(v):.{digits}%}"
    except Exception:
        return "-"


def _fmt_num(v: Any, digits: int = 2) -> str:
    try:
        return f"{float(v):,.{digits}f}"
    except Exception:
        return "-"


def _fmt_stat(v: Any, kind: str = "number") -> str:
    if v is None:
        return "-"
    return _fmt_pct(v) if kind == "pct" else _fmt_num(v, 3)


class ForwardPage(QWidget):
    """
    Pure monitoring page.

    No broker, no production DB, no strategy calculation, no order submission.
    It only reads the persisted forward state/latest JSON files.
    """
    def __init__(self, project_root: Path | str = ".", parent=None):
        super().__init__(parent)
        self.project_root = Path(project_root)
        self._build()
        self.refresh()

    def _build(self):
        root = QVBoxLayout(self)

        header = QHBoxLayout()
        title = QLabel("BreakoutRetest Forward Simulation")
        title.setStyleSheet("font-size:18px;font-weight:bold")
        header.addWidget(title)
        self.status = QLabel("NOT INITIALIZED")
        self.status.setStyleSheet("font-weight:bold;color:#f0ad4e")
        header.addWidget(self.status)
        header.addStretch()
        self.source = QLabel("")
        self.source.setStyleSheet("color:#9aa4b2")
        header.addWidget(self.source)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        root.addLayout(header)

        summary = QGroupBox("Forward 状态")
        sg = QGridLayout(summary)
        self.labels = {}
        items = [
            ("historical_cutoff", "历史截止"),
            ("started_at", "开始日期"),
            ("last_processed_date", "最近处理"),
            ("review_not_before", "最早复审"),
            ("equity", "模拟权益"),
            ("total_return", "累计收益"),
            ("max_drawdown", "最大回撤"),
            ("gross_weight", "当前敞口"),
            ("active_positions", "当前持仓"),
            ("pending_signals", "Pending"),
            ("blocked_count", "Blocked"),
        ]
        for idx, (key, caption) in enumerate(items):
            r, c = divmod(idx, 4)
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(6, 2, 6, 2)
            cap = QLabel(caption)
            cap.setStyleSheet("color:#9aa4b2;font-size:11px")
            val = QLabel("-")
            val.setStyleSheet("font-size:16px;font-weight:bold")
            bl.addWidget(cap)
            bl.addWidget(val)
            self.labels[key] = val
            sg.addWidget(box, r, c)
        root.addWidget(summary)

        progress_box = QGroupBox("正式复审进度")
        pg = QHBoxLayout(progress_box)
        self.progress = QProgressBar()
        self.progress.setMinimum(0)
        self.progress.setMaximum(60)
        self.progress.setValue(0)
        self.progress.setFormat("0 / 60 completed trades")
        pg.addWidget(self.progress, 1)
        self.review_rule = QLabel("且运行时间 ≥ 3个月")
        pg.addWidget(self.review_rule)
        root.addWidget(progress_box)

        stats = QGroupBox("交易统计")
        st = QGridLayout(stats)
        st.addWidget(QLabel("口径"), 0, 0)
        st.addWidget(QLabel("样本"), 0, 1)
        st.addWidget(QLabel("平均收益"), 0, 2)
        st.addWidget(QLabel("中位数"), 0, 3)
        st.addWidget(QLabel("胜率"), 0, 4)
        st.addWidget(QLabel("Profit Factor"), 0, 5)
        self.stat_labels = {}
        for row, (key, title) in enumerate(
            [("base_20bps", "20bp 主口径"), ("shadow_50bps", "50bp Shadow")], start=1
        ):
            st.addWidget(QLabel(title), row, 0)
            self.stat_labels[key] = []
            for col in range(1, 6):
                lab = QLabel("-")
                self.stat_labels[key].append(lab)
                st.addWidget(lab, row, col)
        root.addWidget(stats)

        self.tabs = QTabWidget()
        self.positions = self._table(
            ["代码", "信号日", "入场日", "入场价", "目标权重", "最近收盘", "计划持有"]
        )
        self.pending = self._table(
            ["代码", "信号日", "突破日", "Level", "状态/原因"]
        )
        self.closed = self._table(
            ["代码", "信号日", "入场日", "退出日", "入场价", "退出价", "20bp收益", "50bp收益"]
        )
        self.blocked = self._table(
            ["代码", "信号日", "日期", "原因"]
        )
        self.tabs.addTab(self.positions, "当前模拟持仓")
        self.tabs.addTab(self.pending, "Pending")
        self.tabs.addTab(self.closed, "最近完成交易")
        self.tabs.addTab(self.blocked, "Blocked")
        root.addWidget(self.tabs, 1)

        policy = QGroupBox("冻结策略配置")
        pl = QHBoxLayout(policy)
        self.policy = QLabel("-")
        self.policy.setWordWrap(True)
        self.policy.setStyleSheet("color:#b9c1cc")
        pl.addWidget(self.policy)
        root.addWidget(policy)

    @staticmethod
    def _table(headers):
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        t.setSelectionBehavior(QTableWidget.SelectRows)
        return t

    @staticmethod
    def _fill_table(table: QTableWidget, rows):
        table.setRowCount(len(rows))
        for i, vals in enumerate(rows):
            for j, value in enumerate(vals):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
                table.setItem(i, j, item)

    def refresh(self):
        view = load_forward_view(self.project_root)
        self.source.setText("forward_breakout_v1")

        if not view["available"]:
            self.status.setText(view["status"])
            self.status.setStyleSheet("font-weight:bold;color:#f0ad4e")
            self.policy.setText(
                "; ".join(view.get("errors") or [view.get("message") or ""])
            )
            self._clear_tables()
            return

        status = str(view["status"])
        self.status.setText(status)
        color = (
            "#5cb85c" if status in {"COLLECTING", "REVIEW_DUE"}
            else "#5bc0de" if status == "ARMED_WAIT_NEXT_SESSION"
            else "#f0ad4e"
        )
        self.status.setStyleSheet(f"font-weight:bold;color:{color}")

        values = {
            "historical_cutoff": view.get("historical_cutoff") or "-",
            "started_at": view.get("started_at") or "-",
            "last_processed_date": view.get("last_processed_date") or "-",
            "review_not_before": view.get("review_not_before") or "-",
            "equity": (
                f"¥{view['equity']:,.2f}" if view.get("equity") is not None else "-"
            ),
            "total_return": _fmt_pct(view.get("total_return")),
            "max_drawdown": _fmt_pct(view.get("max_drawdown")),
            "gross_weight": _fmt_pct(view.get("gross_weight")),
            "active_positions": str(view.get("active_positions", 0)),
            "pending_signals": str(view.get("pending_signals", 0)),
            "blocked_count": str(view.get("blocked_count", 0)),
        }
        for key, value in values.items():
            self.labels[key].setText(value)

        done = int(view.get("completed_trades", 0))
        target = max(1, int(view.get("completed_trades_target", 60)))
        self.progress.setMaximum(target)
        self.progress.setValue(min(done, target))
        self.progress.setFormat(f"{done} / {target} completed trades")
        self.review_rule.setText(
            f"且运行时间 ≥ 3个月；最早复审 {view.get('review_not_before') or '-'}"
        )

        for key in ("base_20bps", "shadow_50bps"):
            s = view.get(key) or {}
            vals = [
                str(s.get("n", 0)),
                _fmt_stat(s.get("avg"), "pct"),
                _fmt_stat(s.get("median"), "pct"),
                _fmt_stat(s.get("win_rate"), "pct"),
                _fmt_stat(s.get("profit_factor"), "number"),
            ]
            for lab, val in zip(self.stat_labels[key], vals):
                lab.setText(val)

        pos_rows = []
        for x in view.get("positions") or []:
            pos_rows.append([
                x.get("symbol", "-"),
                x.get("signal_date", "-"),
                x.get("entry_date", "-"),
                _fmt_num(x.get("entry_price")),
                _fmt_pct(x.get("target_weight_at_entry")),
                _fmt_num(x.get("last_close")),
                f"{x.get('planned_exit_session_age', '-')} sessions",
            ])
        self._fill_table(self.positions, pos_rows)

        pending_rows = []
        for x in view.get("pending") or []:
            pending_rows.append([
                x.get("symbol", "-"),
                x.get("signal_date", "-"),
                x.get("breakout_date", "-") or "-",
                _fmt_num(x.get("level")),
                x.get("reason") or x.get("source_state") or "-",
            ])
        self._fill_table(self.pending, pending_rows)

        closed_rows = []
        for x in view.get("closed_trades") or []:
            closed_rows.append([
                x.get("symbol", "-"),
                x.get("signal_date", "-"),
                x.get("entry_date", "-"),
                x.get("exit_date", "-"),
                _fmt_num(x.get("entry_price")),
                _fmt_num(x.get("exit_price")),
                _fmt_pct(x.get("net_return_20bps")),
                _fmt_pct(x.get("net_return_50bps")),
            ])
        self._fill_table(self.closed, closed_rows)

        blocked_rows = []
        for x in view.get("blocked_entries") or []:
            blocked_rows.append([
                x.get("symbol", "-"),
                x.get("signal_date", "-"),
                x.get("date", "-"),
                x.get("reason", "-"),
            ])
        self._fill_table(self.blocked, blocked_rows)

        p = view.get("policy") or {}
        self.policy.setText(
            "BreakoutRetest / RETEST_HOLD | "
            f"单票 { _fmt_pct(p.get('target_weight')) } | "
            f"组合上限 { _fmt_pct(p.get('max_total_weight')) } | "
            f"最大并发 {p.get('max_positions', '-')} | "
            f"退出 {p.get('holding_sessions_from_signal', '-')} sessions | "
            f"主成本 {_fmt_pct(p.get('round_trip_cost'))} | "
            f"Shadow {_fmt_pct(p.get('shadow_round_trip_cost'))}"
        )

    def _clear_tables(self):
        for table in (self.positions, self.pending, self.closed, self.blocked):
            table.setRowCount(0)
