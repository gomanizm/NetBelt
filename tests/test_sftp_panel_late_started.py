# -*- coding: utf-8 -*-
"""停止の後に遅れて届いた started で、SFTP パネルが「起動中」に戻らないことを検証する。

何が起きていたか: started は待受スレッドから出るのでキュー経由で遅れて届き、
stopped は GUI スレッドの stop() から直接届く。待受スレッドが started を出した後、
イベントループが回る前に stop() すると、「停止中」の後に started が配られ、
_on_server_started が状態の表示を「🔵 起動中 (ポート N)」へ戻していた
（実際のサーバーは止まっていて、ボタンは「起動」）。

どう直したか: _on_server_started は、マネージャが動いていない（is_running が
False）なら何もしない。

確かめ方: ネットワークは使わない。待受スレッドの代わりに別スレッドから
started を出し（キュー経由になる）、配送の前後の表示を見る。
"""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


class SftpPanelLateStartedTest(unittest.TestCase):
    _panels = []

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        for _ in range(3):
            cls.app.processEvents()
        cls._panels.clear()

    def setUp(self):
        p = mock.patch("core.config_manager.app_data_dir",
                       return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        p.start()
        self.addCleanup(p.stop)

    def _panel(self):
        from ui.sftp_server_panel import SFTPServerPanel
        panel = SFTPServerPanel()
        self._panels.append(panel)
        return panel

    def _emit_started_from_worker(self, manager):
        t = threading.Thread(target=manager.started.emit)
        t.start()
        t.join()
        for _ in range(5):
            self.app.processEvents()

    def test_a_started_delivered_after_stop_does_not_show_running(self):
        panel = self._panel()
        manager = panel.sftp_server
        manager.is_running = False
        panel._on_server_stopped()
        self._emit_started_from_worker(manager)
        self.assertEqual(panel.status_label.text(), "🔴 停止中",
                         "停止の後に届いた started で「起動中」に戻った")
        self.assertNotIn("サーバー起動", panel.log_text.toPlainText())

    def test_a_started_while_running_still_shows_running(self):
        panel = self._panel()
        manager = panel.sftp_server
        manager.is_running = True
        self.addCleanup(setattr, manager, "is_running", False)
        self._emit_started_from_worker(manager)
        self.assertTrue(panel.status_label.text().startswith("🔵 起動中"))
        self.assertIn("サーバー起動", panel.log_text.toPlainText())


if __name__ == "__main__":
    unittest.main()
