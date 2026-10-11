from __future__ import annotations

from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel


class PlaceholderPage(QWidget):
    def __init__(self, title: str, message: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        title_label = QLabel(title)
        title_label.setStyleSheet("font-size:22px;font-weight:bold")
        message_label = QLabel(message)
        message_label.setWordWrap(True)
        message_label.setStyleSheet("color:#9aa4b2;font-size:13px")
        layout.addWidget(title_label)
        layout.addWidget(message_label)
        layout.addStretch()
