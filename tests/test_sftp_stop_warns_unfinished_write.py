"""srv-01: 書き込み中の転送が残ったまま停止したことを、停止の時点で知らせる件。

何が起きていたか（実測、基準 441ea02。127.0.0.1 のみ。保存先への write() を
合図まで止めて、共有フォルダで止まった状態を模した）: 「サーバー停止」を
押すと stop() は約 1.2 秒で False（書き込みが残った）を返すが、パネルは
戻り値を見ていなかった。ログ欄に増えたのは ['サーバー停止',
'クライアント切断: 127.0.0.1'] で、書き込みの無い普通の停止と見分けが
つかない。残った書き込みは print（exe ではログファイル）にしか出ず、
次に「サーバー起動」を押して初めて「前回の停止が完了していません」の
モーダルで断られて分かった。

利用者の決定（決定 B）: 1.3.1 の「残っている間は再起動を断る」は保ち、
停止の時点でログ欄へ警告を 1 行出す。モーダルは出さない（直後に起動を
押すと断りのモーダルも出て 2 重になる）。SFTP パネルだけ。

どう直したか: SFTPServerPanel._on_stop_server が stop() の戻り値を見て、
False ならログ欄へ警告を 1 行出す。stopped は同じスレッドから同期で届く
ので、ログは「サーバー停止」→「警告: …」の順になる。
"""
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

USER = "tester"
PASSWORD = "example-pass"
WARNING = "終わるまで起動できません"


def free_tcp_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _BlockingWrite:
    """write() が合図まで戻らないファイル（共有フォルダの遅延・切断を模す）"""

    def __init__(self, handle, gate, entered):
        self._handle = handle
        self._gate = gate
        self._entered = entered

    def write(self, data):
        self._entered.set()
        self._gate.wait(30)
        return self._handle.write(data)

    def __getattr__(self, name):
        return getattr(self._handle, name)


class SftpStopWarnsUnfinishedWriteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import ui.sftp_server_panel as panel_mod
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        home = mock.patch("core.config_manager.app_data_dir",
                          return_value=data_dir)
        home.start()
        self.addCleanup(home.stop)
        # モーダルは開かせずに数える（停止の知らせはログだけのはず）
        self.modals = []
        for kind in ("critical", "warning", "information"):
            patcher = mock.patch.object(
                panel_mod.QMessageBox, kind,
                side_effect=lambda *a, _k=kind, **kw: self.modals.append(_k))
            patcher.start()
            self.addCleanup(patcher.stop)

    def _panel(self):
        from ui.sftp_server_panel import SFTPServerPanel
        panel = SFTPServerPanel()
        self.addCleanup(panel.deleteLater)
        self.addCleanup(panel.sftp_server.stop)
        panel.username_edit.setText(USER)
        panel.password_edit.setText(PASSWORD)
        return panel

    def _lines(self, panel):
        return panel.log_text.toPlainText().splitlines()

    def _stop_with_result(self, panel, finished):
        with mock.patch.object(panel.sftp_server, "stop",
                               return_value=finished):
            panel._on_stop_server()

    # --- 戻り値だけを差し替えて確かめる ------------------------------------

    def test_stop_that_leaves_a_writer_says_so_in_the_log(self):
        panel = self._panel()
        self._stop_with_result(panel, finished=False)

        warned = [line for line in self._lines(panel) if WARNING in line]
        self.assertEqual(len(warned), 1,
                         "停止の時点で何も知らせていない（次に起動を押すまで"
                         "分からない）: %r" % self._lines(panel))
        self.assertIn("警告", warned[0])
        self.assertEqual(self.modals, [],
                         "停止の知らせはログだけ（モーダルは出さない）")
        self.assertFalse(panel.start_btn.isHidden(),
                         "知らせても画面は停止の状態へ戻る")
        self.assertTrue(panel.stop_btn.isHidden())

    def test_clean_stop_adds_no_warning(self):
        panel = self._panel()
        self._stop_with_result(panel, finished=True)

        self.assertFalse([line for line in self._lines(panel)
                          if WARNING in line],
                         "普通の停止なのに警告を出している")
        self.assertEqual(self.modals, [])

    # --- 実物: 書き込みを保存先で止めたまま停止を押す ------------------------

    def test_a_stuck_upload_is_reported_right_after_the_stop_line(self):
        import paramiko
        import core.sftp_server as sftp_server

        gate = threading.Event()
        entered = threading.Event()
        self.addCleanup(gate.set)
        real_open = sftp_server.SFTPServerHandler.open

        def patched_open(handler, path, flags, attr):
            fobj = real_open(handler, path, flags, attr)
            if getattr(fobj, "writefile", None) is not None:
                fobj.writefile = _BlockingWrite(fobj.writefile, gate, entered)
            return fobj

        patcher = mock.patch.object(sftp_server.SFTPServerHandler, "open",
                                    patched_open)
        patcher.start()
        self.addCleanup(patcher.stop)

        panel = self._panel()
        port = free_tcp_port()
        panel.port_spin.setValue(port)
        panel.root_dir_edit.setText(
            tempfile.mkdtemp(prefix="netbelt-sftp-stop-warn-"))
        panel._on_start_server()
        deadline = time.time() + 5
        while not panel.sftp_server.is_running and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(panel.sftp_server.is_running, "サーバーが起動しない")

        def upload():
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect("127.0.0.1", port=port, username=USER,
                               password=PASSWORD, look_for_keys=False,
                               allow_agent=False, timeout=10)
                with client.open_sftp().open("config.cfg", "wb") as remote:
                    remote.write(b"OLD-CONTENT")
            except Exception:
                pass   # 停止で切られるのは想定どおり

        threading.Thread(target=upload, daemon=True).start()
        self.assertTrue(entered.wait(20),
                        "アップロードが write() まで進んでいない（前提が崩れている）")
        self.app.processEvents()
        before = len(self._lines(panel))

        panel._on_stop_server()

        added = self._lines(panel)[before:]
        self.assertTrue(added and added[0].endswith("サーバー停止"),
                        "停止の行が先頭にない: %r" % added)
        self.assertTrue(len(added) >= 2 and WARNING in added[1],
                        "停止の直後に警告が出ていない: %r" % added)
        self.assertEqual(self.modals, [],
                         "停止の知らせはログだけ（モーダルは出さない）")


if __name__ == "__main__":
    unittest.main()
