"""起動中に届いた「窓を前へ出せ」の合図を、捨てずに取っておくことを検証する。

実測（334cd72）: main() は listen() → MainWindow() → set_window() の順で
進む。MainWindow.__init__ は設定ファイルの警告で QMessageBox.warning を
出す（src/ui/main_window.py:141-143）。モーダルは入れ子のイベントループを
回すので、その中で QLocalServer.newConnection が処理され、
_on_new_connection → raise_window() が self._window = None のまま走って
何もせずに戻る。合図はそこで消費されるだけで、あとから取り直されない。
検査役の実測（listen() 後・set_window() 前に QEventLoop を回した状態）では、
2 つ目の起動は another_instance_is_running() = True で断られる一方、
1 つ目のログは「raise_window called, _window=None」だけで窓は前へ出ず、
利用者から見ると「2 回目のダブルクリックで何も起きない」状態になる。

利用者の決定（2026-09-20）: 2 つ目の起動を断る。既に動いている NetBelt が
あるときは、2 つ目は起動せず既存のウィンドウを前面に出す。

直し方: raise_window() は窓をまだ覚えていなければ合図を取っておき
（_pending_raise）、set_window() が窓を受け取った時点で取っておいた合図が
あれば、その場で前へ出す。取っておいた合図は 1 回で使い切る（合図が
無いのに勝手に窓が前へ出ないこと）。
"""
import os
import sys
import unittest
import uuid

sys.path.insert(0, "src")


class SingleInstancePendingRaiseTest(unittest.TestCase):
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

    def _window(self):
        from PyQt6.QtWidgets import QWidget
        window = QWidget()
        self.addCleanup(window.close)
        return window

    def _pump(self, times=20):
        for _ in range(times):
            self.app.processEvents()

    def test_a_signal_arriving_before_the_window_exists_is_not_lost(self):
        """窓を渡す前に届いた合図でも、渡した時点で前へ出すこと。"""
        first = self._guard()
        self.assertTrue(first.listen())
        second = self._guard()
        second._name = first._name

        # MainWindow を作っている最中（モーダルが入れ子のイベントループを
        # 回している間）に 2 つ目が起動した状態
        self.assertTrue(second.another_instance_is_running())
        self._pump()
        window = self._window()
        first.set_window(window)
        self._pump()

        self.assertTrue(window.isVisible(),
                        "起動中に届いた合図が捨てられ、窓が前へ出ていない")

    def test_a_window_is_not_raised_without_a_signal(self):
        """合図が無ければ、窓を渡しただけで勝手に前へ出さないこと。"""
        guard = self._guard()
        self.assertTrue(guard.listen())
        window = self._window()

        guard.set_window(window)
        self._pump()

        self.assertFalse(window.isVisible(),
                         "合図が無いのに窓が前へ出ている")

    def test_a_kept_signal_is_used_only_once(self):
        """取っておいた合図は 1 回で使い切ること。"""
        guard = self._guard()
        guard.raise_window()          # 窓がまだ無い＝合図を取っておく
        first_window = self._window()
        guard.set_window(first_window)
        self._pump()
        self.assertTrue(first_window.isVisible(), "前提: 1 回目は前へ出る")

        second_window = self._window()
        guard.set_window(second_window)
        self._pump()

        self.assertFalse(second_window.isVisible(),
                         "使い切ったはずの合図で窓が前へ出ている")


if __name__ == "__main__":
    unittest.main()
