"""test_pending_update_quit の exec() が、戻ったあとに終了要求を残さないことを検証する。

tests/test_pending_update_quit.py の _run_event_loop_with_watchdog は、
QTimer.singleShot(上限, lambda: app.exit(42)) を仕掛けてから app.exec() を
回し、戻ったらそのまま返していた。exec() が先に 0 で戻ってもタイマーは
残り、上限の時刻に exec() の外で app.exit(42) が呼ばれる。Qt はこのとき
スレッドの「終了中」の印を立てたままにするので、同じプロセスでそのあとに
回す QEventLoop.exec() は、自分のタイマーを待たずに -1 ですぐ戻る。

実測（cx5b-check-snmp の test_zz_cx5b_leftover_watchdog.py、62357d1）:
test_pending_update_quit のあとで 2.5 秒イベントを捌いてから、0.5 秒の
タイマー付きで QEventLoop.exec() を回すと、待たずに戻った。後続のファイルの
待ち合わせが、その試験と無関係な理由で壊れる。

見張りを止めただけでは、この実測はまだ落ちた（0.000 秒・戻り値 -1）。
経過を記録すると、test_applying_before_exec_still_ends_the_event_loop の
exec() の中で、表示していたウィンドウが終了要求で閉じられ、Qt が
「最後のウィンドウが閉じた」として終了要求（QEvent.Quit）をもう 1 つ
積んでいた。exec() はその前に抜けるので要求は積まれたまま残り、次に
イベントを捌いた時点で exec() の外の exit(0) になる。結果は見張りと同じ。

直し方: 見張りは QTimer を作って仕掛け、exec() から戻る前に stop() する。
あわせて、exec() の中で積まれて残った QEvent.Quit を戻る前に取り除く。
どちらも戻り値と経過秒を決めたあとの後始末で、判定は変えない。

ここでは同じ手順を直接呼び、exec() を見張りより先に 0 で終わらせてから
上限を過ぎるまでイベントを捌き、別の QEventLoop が自分のタイマーまで
回るかを見る。積まれて残る終了要求は、ウィンドウを閉じる代わりに
exec() の中で直接積む（ほかのテストのウィンドウを閉じないため）。
"""
import os
import time
import types
import unittest

# tests/ は pytest が sys.path に入れる。クラスではなくモジュールを取り込み、
# 向こうのテストをここで二重に集めないようにする。
import test_pending_update_quit as quit_tests


class PendingUpdateWatchdogTimerTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _reset_quit_state(self):
        """exec() の外で exit() が呼ばれた印を、app.exec() を一度回して消す。"""
        from PyQt6.QtCore import QCoreApplication, QEvent, QTimer
        app = self.app
        QCoreApplication.removePostedEvents(app, QEvent.Type.Quit)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: app.exit(0))
        timer.start(0)
        app.exec()
        timer.stop()

    def _loop_runs_for(self, msec):
        """自分のタイマーで抜ける QEventLoop を回し、経過秒を返す。"""
        from PyQt6.QtCore import QEventLoop, QTimer
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(msec)
        started = time.monotonic()
        loop.exec()
        elapsed = time.monotonic() - started
        timer.stop()
        return elapsed

    def _run_helper_ending_early(self, on_exec):
        """見張り 0.3 秒で手順を回し、exec() の中で on_exec を呼んで先に終わらせる。"""
        from PyQt6.QtCore import QTimer
        early = QTimer()
        early.setSingleShot(True)
        early.timeout.connect(on_exec)
        early.start(0)
        code, _ = quit_tests.PendingUpdateQuitTest._run_event_loop_with_watchdog(
            types.SimpleNamespace(app=self.app), 0.3)
        early.stop()
        self.assertEqual(code, 0, "前提が崩れている（exec() が先に戻っていない）")

        # 見張りの上限（0.3 秒）を過ぎるまでイベントを捌く
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)

        elapsed = self._loop_runs_for(500)
        if elapsed < 0.4:
            # 後続のテストを巻き込まないよう、印を消してから落とす
            self._reset_quit_state()
        return elapsed

    def test_the_watchdog_does_not_fire_after_exec_returned(self):
        """exec() が先に戻ったら、見張りがあとから exit(42) を呼ばないこと。"""
        app = self.app
        elapsed = self._run_helper_ending_early(lambda: app.exit(0))
        self.assertGreater(
            elapsed, 0.4,
            "見張りが exec() の外で発火し、次の QEventLoop.exec() が %.3f 秒で"
            "戻った" % elapsed)

    def test_a_quit_queued_inside_exec_does_not_outlive_it(self):
        """exec() の中で積まれて残った終了要求を、戻る前に片付けること。"""
        from PyQt6.QtCore import QCoreApplication, QEvent
        app = self.app

        def quit_leaving_one_queued():
            # 最後のウィンドウが閉じたときに Qt が積むのと同じ要求
            QCoreApplication.postEvent(app, QEvent(QEvent.Type.Quit))
            app.exit(0)

        elapsed = self._run_helper_ending_early(quit_leaving_one_queued)
        self.assertGreater(
            elapsed, 0.4,
            "exec() の中で積まれた終了要求が残り、次の QEventLoop.exec() が"
            " %.3f 秒で戻った" % elapsed)


if __name__ == "__main__":
    unittest.main()
