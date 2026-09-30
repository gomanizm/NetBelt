"""srv-01: 停止の時点の警告が、転送・削除・改名のほかに止まったファイル操作も言い表す件。

何が起きていたか（800fcb0 と 1131006 のあと。127.0.0.1 のみ）: 停止が生き残りと
して数える要求に、SETSTAT（切り詰め・日時・属性）と mkdir・rmdir が加わった
（_OpenWriters.busy）。ところがパネルの警告は「書き込み中だった転送（または
削除・名前の変更）が終わっていません。保存先のファイルを掴んだままの可能性が
あり…」のままで、止まったのが属性の変更やフォルダの作成のときに、起きて
いないことを言っていた。

どう直したか: 警告の文言を「書き込み中だった転送（または削除・名前の変更などの
ファイル操作）が終わっていません。保存先を掴んだままの可能性があり、終わるまで
起動できません」にした。出す条件（stop() が False）と出し方（ログ欄へ 1 行、
モーダルなし）は変えない。

確かめ方: 保存先への os.chmod / os.mkdir を合図まで止め（共有フォルダの遅延を
模す）、止まったまま停止を押す。停止の行の直後に警告が 1 行出て、その文言が
ファイル操作を含む言い方になっていることを見る。
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
TARGET = "config.cfg"
FOLDER = "slowdir"


def free_tcp_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class SftpStopWarningNamesOtherOperationsTest(unittest.TestCase):
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
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-stop-warn-ops-")
        self.gate = threading.Event()      # 止めた操作を通す合図
        self.entered = threading.Event()   # 操作が止まった合図
        self.addCleanup(self.gate.set)     # 生き残りを解放してから後片付けへ

    def _block(self, name, suffix):
        """os.<name> を、suffix で終わるパスに対してだけ合図まで止める"""
        real = getattr(os, name)
        gate, entered = self.gate, self.entered

        def blocking(path, *args, **kwargs):
            if str(path).endswith(suffix):
                entered.set()
                gate.wait(30)
            return real(path, *args, **kwargs)

        patcher = mock.patch.object(os, name, blocking)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _panel(self):
        from ui.sftp_server_panel import SFTPServerPanel
        panel = SFTPServerPanel()
        self.addCleanup(panel.deleteLater)
        self.addCleanup(panel.sftp_server.stop)
        panel.username_edit.setText(USER)
        panel.password_edit.setText(PASSWORD)
        self.port = free_tcp_port()
        panel.port_spin.setValue(self.port)
        panel.root_dir_edit.setText(self.root)
        panel._on_start_server()
        deadline = time.time() + 5
        while not panel.sftp_server.is_running and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(panel.sftp_server.is_running, "サーバーが起動しない")
        return panel

    def _lines(self, panel):
        return panel.log_text.toPlainText().splitlines()

    def _send(self, action):
        """別スレッドのクライアントから 1 件送る（停止で切られるのは想定どおり）"""
        import paramiko

        def run():
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect("127.0.0.1", port=self.port, username=USER,
                               password=PASSWORD, look_for_keys=False,
                               allow_agent=False, timeout=10)
                action(client.open_sftp())
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _assert_warned_after_stuck(self, panel, what):
        self.assertTrue(self.entered.wait(20),
                        "%s がサーバー側で止まっていない（前提が崩れている）" % what)
        self.app.processEvents()
        before = len(self._lines(panel))

        panel._on_stop_server()

        added = self._lines(panel)[before:]
        self.assertTrue(added and added[0].endswith("サーバー停止"),
                        "停止の行が先頭にない: %r" % added)
        self.assertTrue(len(added) >= 2 and WARNING in added[1],
                        "止まった %s を停止の時点で知らせていない: %r"
                        % (what, added))
        self.assertIn("ファイル操作", added[1],
                      "警告が転送・削除・改名だけを言い、止まった %s に"
                      "当てはまらない: %r" % (what, added[1]))
        self.assertEqual(self.modals, [],
                         "停止の知らせはログだけ（モーダルは出さない）")

    # --- 本題 ------------------------------------------------------------

    def test_a_stuck_setstat_is_warned_as_a_file_operation(self):
        with open(os.path.join(self.root, TARGET), "wb") as handle:
            handle.write(b"OLD-CONTENT")
        self._block("chmod", TARGET)
        panel = self._panel()
        self._send(lambda sftp: sftp.chmod(TARGET, 0o444))
        self._assert_warned_after_stuck(panel, "SETSTAT（chmod）")

    def test_a_stuck_mkdir_is_warned_as_a_file_operation(self):
        self._block("mkdir", FOLDER)
        panel = self._panel()
        self._send(lambda sftp: sftp.mkdir(FOLDER))
        self._assert_warned_after_stuck(panel, "mkdir")


if __name__ == "__main__":
    unittest.main()
