"""「今すぐ更新」で終わるとき、記録中ダイアログより先に主窓を閉じることを検証する。

何が起きていたか（6cf811c。検査役の実測: scratchpad\\cx132-termui-verify\\
probe_update_order.py、offscreen・偽の SSH）: 記録中ダイアログは、閉じると
「記録停止」と同じく記録を止める（利用者の決定 (ii)。見えている ⇔ 記録中）。
更新ダイアログからの終了（quit_for_update）は QApplication.closeAllWindows()
で表示中の窓を閉じるが、その並びは実行ごとに変わり、6 回中 3 回はダイアログが
主窓より先に閉じられた。そのときは主窓の closeEvent（接続を切ってから配送待ちの
受信を取り込み、記録を書き切る）より前に記録が止まり、次の分が記録から欠けた。
  - 終了を押した時点で、受信スレッドが送ってまだ配られていない分
  - closeEvent の途中（サーバの停止・MIB 読み込み待ち、最大 10 秒）に届いた分
441ea02 では並びによらず全部入っていた（ダイアログを閉じても記録は続いた）。
既存の tests/test_update_quit_finishes_log_recording.py は描き待ちの分しか
見ないので、この欠けを捉えない（描き待ちは止めるときにも書き切られる）。

どう直したか: quit_for_update(window=None) で、closeAllWindows の前に表示中の
主窓（QMainWindow）を閉じる。主窓の closeEvent が記録を書き切ってから
ダイアログを捨てるので、closeAllWindows の時点でダイアログはもう見えておらず、
並びによらず同じ結果になる。ダイアログの振る舞い（閉じたら止める）は変えない。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

sys.path.insert(0, "src")


class FakeSSH(QObject):
    """接続の見た目だけを持つ偽物。connect() は即座に成功を知らせる。"""
    output_received = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error_occurred = pyqtSignal(str)
    instances = []

    def __init__(self, host, port, username, password, ssh_key, parent=None):
        super().__init__(parent)
        self.client = None
        self.sent = []
        FakeSSH.instances.append(self)

    def set_terminal_size(self, cols, rows):
        pass

    def connect(self):
        self.connected.emit()
        return True

    def send_command(self, command):
        self.sent.append(command)

    def dispose(self):
        pass

    def disconnect(self):
        pass


def _emit_from_thread(emit):
    """別スレッドから emit して、戻るまで待つ（配送はまだされない）"""
    thread = threading.Thread(target=emit)
    thread.start()
    thread.join(5)


def _close_all_windows_dialog_first():
    """QApplication.closeAllWindows の代わり。記録中ダイアログを先に閉じる。

    Qt は表示中のトップレベルを 1 つずつ QWindow.close() で閉じ、閉じるたびに
    一覧を取り直す（QApplicationPrivate::tryCloseAllWidgetWindows）。一覧の
    並びは実行ごとに変わるので、ダイアログが先に来る並びをここで固定する。
    """
    from ui.dialogs.log_recording_dialog import LogRecordingDialog
    for _ in range(20):
        shown = [w for w in QApplication.topLevelWidgets() if w.isVisible()]
        if not shown:
            return
        shown.sort(key=lambda w: not isinstance(w, LogRecordingDialog))
        if not shown[0].windowHandle().close():
            return


class UpdateQuitClosesTheMainWindowFirstTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "dev")
        self.dir = tempfile.mkdtemp(prefix="netbelt-update-order-")
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(self.dir))
        ap.start()
        self.addCleanup(ap.stop)
        FakeSSH.instances = []
        ssh_patch = mock.patch("ui.main_window.SSHConnection", FakeSSH)
        ssh_patch.start()
        self.addCleanup(ssh_patch.stop)
        # offscreen ではモーダルを閉じる相手がいない（出たら失敗として見る）
        warn = mock.patch("PyQt6.QtWidgets.QMessageBox.warning")
        self.warning = warn.start()
        self.addCleanup(warn.stop)
        crit = mock.patch("PyQt6.QtWidgets.QMessageBox.critical")
        self.critical = crit.start()
        self.addCleanup(crit.stop)

    def _pump(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    @staticmethod
    def _discard(window):
        """描き残しを持ったまま次のテストへ行かない（タイマーで描き始める）"""
        window.terminal_widget._output_timer.stop()
        window.terminal_widget._pending_output.clear()
        window.close()

    def _recording_window(self):
        """dev に接続して記録を始めた、表示中のメインウィンドウと記録先のパス"""
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(self.dir, "config.json"))
            window = MainWindow()
        self.addCleanup(self._discard, window)
        window.show()
        device = {"name": "dev", "host": "192.0.2.10", "port": 22,
                  "username": "u", "password": "", "protocol": "ssh"}
        window.device_tree.load_from_config(
            [{"name": "Lab", "devices": [device]}])
        window._on_connect_requested(device)
        self._pump()
        log_path = os.path.join(self.dir, "dev.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(log_path, "")):
            window._on_start_log_recording()
        tw = window.terminal_widget
        self.assertIn("dev", tw._log_files, "前提: 記録中")
        self.assertTrue(tw._log_dialogs["dev"].isVisible(),
                        "前提: 記録中ダイアログが見えている")
        return window, log_path

    def _quit_for_update_with_output(self, close_all=None):
        """記録中に「今すぐ更新」の終了を通し、記録の中身と控えを返す。

        終了を押す直前に受信スレッドが送った分（未配送）と、主窓の
        closeEvent が MIB 読み込み待ちで固まっている間に届いた分を作る。
        """
        from ui.dialogs.update_dialog import quit_for_update
        from ui.main_window import MainWindow
        from ui.snmp_panel import SNMPPanel
        window, log_path = self._recording_window()
        tw = window.terminal_widget
        conn = FakeSSH.instances[-1]
        conn.output_received.emit("DRAWN-BEFORE\r\n")
        self._pump()
        _emit_from_thread(
            lambda: conn.output_received.emit("UNDELIVERED-BEFORE\r\n"))
        inflight = ["INFLIGHT %02d" % i for i in range(5)]

        def during_close(_panel):
            _emit_from_thread(
                lambda: [conn.output_received.emit(x + "\r\n")
                         for x in inflight])

        entered = []
        original = MainWindow.closeEvent

        def spy(this, event):
            entered.append("dev" in tw._log_files)
            original(this, event)

        patches = [mock.patch.object(SNMPPanel, "wait_for_background_work",
                                     during_close),
                   mock.patch.object(MainWindow, "closeEvent", spy),
                   mock.patch.object(QApplication, "quit")]
        if close_all is not None:
            patches.append(mock.patch.object(QApplication, "closeAllWindows",
                                             close_all))
        for p in patches:
            p.start()
        try:
            quit_for_update()
        finally:
            for p in reversed(patches):
                p.stop()
        with open(log_path, encoding="utf-8") as f:
            recorded = f.read()
        return window, recorded, inflight, entered

    def _assert_everything_recorded(self, window, recorded, inflight, entered):
        tw = window.terminal_widget
        self.assertEqual(entered, [True],
                         "主窓の closeEvent より前に記録が止まった")
        self.assertIn("DRAWN-BEFORE", recorded, "前提: 描いた分は入っている")
        self.assertIn("UNDELIVERED-BEFORE", recorded,
                      "終了を押した時点で未配送だった受信が記録から欠けた")
        missing = [x for x in inflight if x not in recorded]
        self.assertEqual(missing, [],
                         "閉じる途中に届いた受信が記録から欠けた")
        self.assertNotIn("dev", tw._log_files, "記録が止まっていない")
        self.assertNotIn("dev", tw._log_dialogs,
                         "記録中ダイアログが残っている")
        self.warning.assert_not_called()
        self.critical.assert_not_called()

    def test_the_dialog_closed_first_does_not_cut_the_recording(self):
        """closeAllWindows がダイアログを先に拾う並びでも、記録を書き切ること。"""
        window, recorded, inflight, entered = self._quit_for_update_with_output(
            _close_all_windows_dialog_first)
        self._assert_everything_recorded(window, recorded, inflight, entered)

    def test_the_real_close_all_windows_keeps_the_whole_recording(self):
        """本物の closeAllWindows を通しても、記録を書き切ること。"""
        window, recorded, inflight, entered = self._quit_for_update_with_output()
        self._assert_everything_recorded(window, recorded, inflight, entered)
        self.assertFalse(window.isVisible(), "主窓が閉じていない")


if __name__ == "__main__":
    unittest.main()
