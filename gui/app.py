from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run_gui(config_path="config/settings.yaml"):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont
    from core.config import Settings
    from core.logging_setup import setup_logging
    from .dashboard import Dashboard

    s = Settings(config_path)
    setup_logging(s.get("app.log_dir", "logs"))
    app = QApplication.instance() or QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei", 10))
    w = Dashboard(config_path)
    w.show()
    return app.exec()


if __name__ == "__main__":
    run_gui()
