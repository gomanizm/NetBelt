"""NetBelt を 2 つ起動できないこと（2 つ目は既存の窓を前へ出す）を検証する。

実測（81664d2）: ConfigManager.save_config() は self.config 全体を json.dump
するだけで、読み込み以降のディスクの更新を一切見ていない。検査役の実測
（ConfigManager を 2 つ作り、B の読み込み後に A が add_device / add_group を
実行してから B が _save_layout 相当を 1 回呼ぶ）では、出力は
『ディスク上のグループ: [Default, 本番環境, 検証環境] / ディスク上の機器:
[ルータA] / => A が追加した機器とグループは残っているか: False False』。
逆向きも同じで、A が set_last_check_time で保存すると B が書いた ui_layout が
None に戻る。消えるのは機器・パスワード・グループ・マクロで、警告も
バックアップも出ない。known_hosts には _known_hosts_file_lock による
プロセス間排他があるのに、config.json には無く、多重起動を止める仕組み
（QSharedMemory / QLocalServer / 名前付きミューテックス）も src 配下に無かった。

利用者の決定（2026-09-20）: 2 つ目の起動を断る。既に動いている NetBelt が
あるときは、2 つ目は起動せず既存のウィンドウを前面に出す（標準ライブラリか
PyQt6 の範囲で行い、依存は足さない）。凍結 exe とソース実行の両方で効くこと。
異常終了のあとに残った印で起動できなくならないこと。

直し方: PyQt6 の QLocalServer / QLocalSocket で、利用者ごとの名前を先に
取った方が本体になる。2 つ目は名前へ繋げた時点で「既に動いている」と分かる
ので、繋いで切るだけで終了し、本体側はその接続を合図にウィンドウを前へ出す。
名前は listen の前に removeServer で消すので、異常終了で残った印があっても
起動できなくならない。名前を取れなかったときは断らずそのまま起動する
（止める方が影響が大きい）。main() から使うので、凍結 exe でも同じに効く。
"""
import os
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, "src")


class SingleInstanceGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _guard(self):
        from core.single_instance import SingleInstanceGuard
        guard = SingleInstanceGuard(name="netbelt-test-%s" % uuid.uuid4().hex)
        self.addCleanup(guard.close)
        return guard

    def _pump(self, times=20):
        for _ in range(times):
            self.app.processEvents()

    def test_the_first_start_takes_the_name(self):
        """最初の起動は断られず、名前を取れること。"""
        guard = self._guard()

        self.assertFalse(guard.another_instance_is_running(),
                         "誰も動いていないのに断られている")
        self.assertTrue(guard.listen(), "名前を取れていない")

    def test_the_second_start_is_refused(self):
        """名前が取られていれば、2 つ目は「動いている」と分かること。"""
        first = self._guard()
        self.assertTrue(first.listen())
        second = self._guard()
        second._name = first._name

        self.assertTrue(second.another_instance_is_running(),
                        "2 つ目が起動できてしまう")

    def test_the_second_start_wakes_the_first_window(self):
        """2 つ目の起動が、動いている方のウィンドウを前へ出させること。"""
        first = self._guard()
        self.assertTrue(first.listen())
        first.raise_window = mock.Mock()
        second = self._guard()
        second._name = first._name

        second.another_instance_is_running()
        self._pump()

        self.assertTrue(first.raise_window.called,
                        "既存のウィンドウを前へ出していない")

    def test_raising_restores_a_minimized_window(self):
        """最小化されていても、前へ出すこと。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QWidget
        guard = self._guard()
        window = QWidget()
        self.addCleanup(window.close)
        window.showMinimized()
        self._pump()
        guard.set_window(window)

        guard.raise_window()
        self._pump()

        self.assertFalse(
            bool(window.windowState() & Qt.WindowState.WindowMinimized),
            "最小化されたままになっている")
        self.assertTrue(window.isVisible(), "表示されていない")

    def test_a_leftover_name_is_cleared_before_listening(self):
        """異常終了で残った印があっても起動できなくならないこと。"""
        from PyQt6.QtNetwork import QLocalServer
        guard = self._guard()
        removed = []

        with mock.patch.object(QLocalServer, "removeServer",
                               side_effect=removed.append):
            self.assertTrue(guard.listen())

        self.assertIn(guard._name, removed,
                      "listen の前に残った印を消していない")

    def test_a_failure_to_take_the_name_does_not_raise(self):
        """名前を取れなくても、例外で起動を止めないこと。"""
        from PyQt6.QtNetwork import QLocalServer
        guard = self._guard()

        with mock.patch.object(QLocalServer, "listen", return_value=False):
            self.assertFalse(guard.listen())

    def test_closing_releases_the_name(self):
        """終了したら名前を手放し、次の起動が断られないこと。"""
        first = self._guard()
        self.assertTrue(first.listen())
        name = first._name
        first.close()

        second = self._guard()
        second._name = name
        self.assertFalse(second.another_instance_is_running(),
                         "終了したのに名前が残っている")


class MainStartupTest(unittest.TestCase):
    """main() が、2 つ目の起動でウィンドウを作らないこと。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        hook = sys.excepthook
        self.addCleanup(lambda: setattr(sys, "excepthook", hook))

    def _run_main(self, another_is_running):
        import main as netbelt_main
        import ui.main_window
        import core.single_instance
        with mock.patch("PyQt6.QtWidgets.QApplication"), \
             mock.patch.object(ui.main_window, "MainWindow") as window_cls, \
             mock.patch.object(core.single_instance,
                               "SingleInstanceGuard") as guard_cls, \
             mock.patch.object(sys, "exit"):
            guard = guard_cls.return_value
            guard.another_instance_is_running.return_value = another_is_running
            netbelt_main.main()
        return window_cls, guard

    def test_a_second_start_opens_no_window(self):
        window_cls, guard = self._run_main(True)

        self.assertFalse(window_cls.called,
                         "2 つ目の起動でウィンドウを作っている")

    def test_the_first_start_opens_the_window_and_takes_the_name(self):
        window_cls, guard = self._run_main(False)

        self.assertTrue(window_cls.called, "ウィンドウが作られていない")
        self.assertTrue(guard.listen.called, "名前を取っていない")
        self.assertTrue(guard.set_window.called,
                        "前へ出すウィンドウを渡していない")


if __name__ == "__main__":
    unittest.main()
