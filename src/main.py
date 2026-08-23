import sys
import os
from PyQt6.QtWidgets import QApplication
from ui.main_window import MainWindow


def _setup_logging():
    """凍結(exe)ビルド時は stdout/stderr をログファイルへ退避する。
    windowedビルドでは print 出力先が無いため、診断用にファイルへ残す。
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        from datetime import datetime
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        log_dir = os.path.join(base, "NetBelt", "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"app_{datetime.now():%Y%m%d}.log")
        f = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = f
        sys.stderr = f
    except Exception:
        pass


def main():
    _setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("NetBelt")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
