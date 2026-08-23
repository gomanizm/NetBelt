"""テスト共通の設定。

このリポジトリのテストは QApplication と QObject（各サーバのマネージャや UI
パネル）を多数作る。PyQt では QApplication の Python 側参照が失われると、
残っているオブジェクトの後片付けで解放済みの C++ オブジェクトに触れ、
プロセスごと落ちることがある（実測で access violation）。

そのため QApplication はセッション全体で1つだけ作り、最後まで保持する。
"""
import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session", autouse=True)
def qapp():
    """セッション全体で1つの QApplication を保持する。"""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])

    yield app

    # 終了前に残ったイベントを捌く
    for _ in range(3):
        app.processEvents()

    # 停止し損ねたスレッドがあれば知らせる（見逃すと終了時に落ちる原因になる）
    leftovers = [t for t in threading.enumerate()
                 if t is not threading.main_thread() and t.is_alive() and not t.daemon]
    if leftovers:
        print("\n[conftest] 終了時に非デーモンスレッドが残っています: %s"
              % ", ".join(t.name for t in leftovers))
