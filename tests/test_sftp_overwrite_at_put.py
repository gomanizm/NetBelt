"""確認を経ていないアップロードが、既存のリモートファイルを黙って潰さないことを検証する。

上書き確認はパネルが「最後に観測した一覧」（_current_entries）で判定していた。
その一覧は送信先ディレクトリと対応づいていないので、
  3a: ファイル選択ダイアログの間に別ディレクトリの一覧が届く
  3b: 接続直後、初回の一覧が届く前に送る
  3c: 同じ名前を続けて送り、間に古い一覧が届いて送信中の記憶が消える
のどれでも「無い」と誤判定し、固定先 /A の config.cfg を確認なしで上書きした
（3 経路とも実測: question called: False / upload_file('/A/config.cfg')）。

パネル側の判定は残しつつ、確認を経ていない送信はマネージャが送る直前に
ロック内でリモートを確かめ、あれば上書きせずエラーで止める。ロック内なら
先行する転送も終わっているので、一覧より新しい状態を見られる。
"""
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SftpManagerRefusesUnconfirmedOverwriteTest(unittest.TestCase):
    """マネージャ側: overwrite を明示されない限り既存のリモートを潰さない。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-ovw-")
        self.local = os.path.join(self.dir, "config.cfg")
        with open(self.local, "wb") as f:
            f.write(b"NEW")

    def _manager(self):
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        m.is_connected = True
        m.sftp_client = mock.Mock()
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.done = [], []
        m.error_occurred.connect(self.errors.append)
        m.transfer_complete.connect(self.done.append)
        return m

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_an_existing_remote_file_is_not_overwritten_without_confirmation(self):
        m = self._manager()
        # stat が通る = リモートに何かある
        m.sftp_client.stat.return_value = mock.Mock()

        m.upload_file(self.local, "/A/config.cfg")

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        m.sftp_client.put.assert_not_called()
        self.assertEqual(self.done, [], "上書きしたうえで完了を通知している")
        self.assertTrue(any("既にあります" in e for e in self.errors), self.errors)

    def test_a_confirmed_upload_replaces_the_existing_file(self):
        m = self._manager()
        m.sftp_client.stat.return_value = mock.Mock()

        m.upload_file(self.local, "/A/config.cfg", overwrite=True)

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        self.assertEqual(self.errors, [])
        m.sftp_client.put.assert_called_once()

    def test_a_stat_that_fails_for_another_reason_is_not_read_as_missing(self):
        """「無い」以外の理由で stat が失敗したら、送らずに理由ごと知らせること。

        stat を権限エラーで返す機器では、失敗を一律「無い」と読むと既存を
        黙って潰す（実測: stat=PermissionError で put called: True, errs: []）。
        """
        m = self._manager()
        m.sftp_client.stat.side_effect = PermissionError("Permission denied")

        m.upload_file(self.local, "/A/config.cfg")

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        m.sftp_client.put.assert_not_called()
        self.assertEqual(self.done, [], "確認できていないのに完了を通知している")
        self.assertTrue(any("Permission denied" in e for e in self.errors),
                        self.errors)

    def test_a_probe_that_times_out_refuses_and_says_so(self):
        """stat が期限切れでも「無い」とは読まず、理由の書かれた拒否にすること。"""
        m = self._manager()
        # 期限切れの確認は使えなくなった接続を畳むので、先に参照を控える
        client = m.sftp_client
        client.stat.side_effect = TimeoutError()   # socket.timeout は str が空

        m.upload_file(self.local, "/A/config.cfg")

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        client.put.assert_not_called()
        self.assertTrue(any("応答しません" in e for e in self.errors), self.errors)

    def test_a_name_taken_by_a_remote_directory_is_not_an_overwrite(self):
        """同名がディレクトリなら、専用の文言で断ること。

        一覧を更新しても種別は変わらないので、「一覧を更新してやり直せ」と
        案内すると利用者は抜けられない。
        """
        m = self._manager()
        attr = mock.Mock()
        attr.st_mode = 0o040755          # ディレクトリ
        m.sftp_client.stat.return_value = attr

        m.upload_file(self.local, "/A/adir")

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        m.sftp_client.put.assert_not_called()
        self.assertTrue(any("ディレクトリ" in e for e in self.errors), self.errors)
        self.assertFalse(any("一覧を更新" in e for e in self.errors),
                         "更新しても解決しない案内を出している: %s" % self.errors)

    def test_a_missing_file_reported_without_a_message_is_uploaded(self):
        """FileNotFoundError なら、文面に関係なく「無い」と読んでよい。"""
        m = self._manager()
        m.sftp_client.stat.side_effect = FileNotFoundError("gone")

        m.upload_file(self.local, "/A/new.cfg")

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        self.assertEqual(self.errors, [])
        m.sftp_client.put.assert_called_once()

    def test_a_generic_stat_failure_still_counts_as_missing(self):
        """理由の分からない失敗は「無い」のまま（新しい名前を送れなくしない）。

        見つからないときの応答は機器によって違う。NetBelt 同梱の SFTP サーバ
        でさえ、無いファイルの stat に SFTP_FAILURE を返す（クライアント側は
        IOError("Failure")）。ここを締めると初回の送信が全部断られる。
        """
        m = self._manager()
        m.sftp_client.stat.side_effect = IOError("Failure")

        m.upload_file(self.local, "/A/new.cfg")

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        self.assertEqual(self.errors, [])
        m.sftp_client.put.assert_called_once()

    def test_a_new_name_is_uploaded_without_confirmation(self):
        m = self._manager()
        m.sftp_client.stat.side_effect = IOError("No such file")

        m.upload_file(self.local, "/A/new.cfg")

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        self.assertEqual(self.errors, [])
        m.sftp_client.put.assert_called_once()


class SftpPanelPassesConfirmationTest(unittest.TestCase):
    """パネル側: 確認を経たときだけ overwrite=True を渡す。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self, sftp_settings=None):
        from core.config_manager import ConfigManager
        from ui.sftp_panel import SFTPPanel
        d = tempfile.mkdtemp(prefix="netbelt-sftpovw-")
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        if sftp_settings is not None:
            cm.set_server_settings("sftp", sftp_settings)
        panel = SFTPPanel(config_manager=cm)
        panel.sftp_manager = mock.Mock()
        panel.sftp_manager.get_current_path.return_value = "/A"
        return panel

    @staticmethod
    def _entry(name, is_dir=False):
        return {"name": name, "is_dir": is_dir, "size": 10, "mode": 0o100644,
                "permissions": "-rw-r--r--", "mtime": 0}

    def _overwrite_flag(self, manager):
        """パネルがマネージャへ渡した上書きの可否を返す。

        キーワードの有無まで見る。kwargs.get の既定値で済ませると、
        overwrite を渡さない（＝受け手の既定値まかせ）実装でも assertFalse が
        通ってしまい、修正の前後で結果が変わらない。
        """
        manager.upload_file.assert_called_once()
        kwargs = manager.upload_file.call_args.kwargs
        self.assertIn("overwrite", kwargs,
                      "upload_file に overwrite が渡されていない: %r"
                      % (manager.upload_file.call_args,))
        return kwargs["overwrite"]

    def test_3a_listing_of_another_directory_during_the_dialog(self):
        """ダイアログ中に /B の一覧が届いても、/A への送信は確認なしの扱いのまま。"""
        from ui import sftp_panel as mod
        panel = self._panel()
        manager = panel.sftp_manager
        panel._update_file_list([self._entry("config.cfg")])   # /A の一覧

        def listing_for_b_arrives(*args, **kwargs):
            manager.get_current_path.return_value = "/B"
            panel._update_file_list([])
            return ("C:/tmp/config.cfg", "")

        with mock.patch.object(mod.QFileDialog, "getOpenFileName",
                               side_effect=listing_for_b_arrives), \
             mock.patch.object(mod.QMessageBox, "question",
                               return_value=mod.QMessageBox.StandardButton.Yes) as q:
            panel._on_upload()

        # 確認が出たなら overwrite=True でよい。出なかったなら、マネージャに
        # 上書きを許してはいけない
        flag = self._overwrite_flag(manager)
        self.assertEqual(manager.upload_file.call_args.args,
                         ("C:/tmp/config.cfg", "/A/config.cfg"))
        if q.called:
            self.assertTrue(flag)
        else:
            self.assertFalse(flag, "確認なしの送信に上書きを許している")

    def test_3b_upload_before_the_first_listing_is_not_an_overwrite_grant(self):
        panel = self._panel()
        with mock.patch("ui.sftp_panel.QMessageBox.question") as q:
            panel._upload_with_confirmation("C:/tmp/config.cfg")
        q.assert_not_called()
        self.assertFalse(self._overwrite_flag(panel.sftp_manager),
                         "一覧を見ていないのに上書きを許している")

    def test_a_confirmed_overwrite_is_granted(self):
        from PyQt6.QtWidgets import QMessageBox
        panel = self._panel()
        panel._update_file_list([self._entry("config.cfg")])
        with mock.patch("ui.sftp_panel.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes):
            panel._upload_with_confirmation("C:/tmp/config.cfg")
        self.assertTrue(self._overwrite_flag(panel.sftp_manager),
                        "利用者が上書きを承認したのに、マネージャが止めてしまう")

    def test_overwrite_is_granted_when_confirmation_is_disabled(self):
        """confirm_overwrite=False は「聞かずに上書き」の意思なので許す。"""
        panel = self._panel({"confirm_overwrite": False})
        panel._update_file_list([self._entry("config.cfg")])
        with mock.patch("ui.sftp_panel.QMessageBox.question") as q:
            panel._upload_with_confirmation("C:/tmp/config.cfg")
        q.assert_not_called()
        self.assertTrue(self._overwrite_flag(panel.sftp_manager))


class SftpOverwriteEndToEndTest(unittest.TestCase):
    """実際のループバック SFTP サーバで、既存ファイルが残ることを見る。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from pathlib import Path
        fw = mock.patch("core.firewall.ensure_inbound_allow",
                        return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        ap = mock.patch("core.config_manager.app_data_dir", return_value=data_dir)
        ap.start()
        self.addCleanup(ap.stop)

    def _server(self):
        from core.sftp_server import SFTPServerManager
        root = tempfile.mkdtemp(prefix="netbelt-ovw-")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        server = SFTPServerManager()
        if not server.start(port=port, root_dir=root,
                            username="netbelt", password="netbelt-pass"):
            self.skipTest("SFTP サーバを起動できない")
        self.addCleanup(server.stop)
        deadline = time.time() + 15
        while not server.is_running and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(server.is_running, "SFTP サーバが待ち受けにならない")
        return root, port

    def _manager(self, port):
        import paramiko
        from core.sftp_manager import SFTPManager
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        deadline = time.time() + 10
        while True:
            try:
                client.connect("127.0.0.1", port=port, username="netbelt",
                               password="netbelt-pass", look_for_keys=False,
                               allow_agent=False, timeout=5)
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(0.2)
        self.addCleanup(client.close)
        m = SFTPManager()
        self.assertTrue(m.connect(client), "SFTP に接続できない")
        self.addCleanup(m.disconnect)
        return m

    def _wait(self, predicate, seconds=15.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_unconfirmed_upload_leaves_the_existing_file_and_says_so(self):
        root, port = self._server()
        m = self._manager(port)
        existing = os.path.join(root, "config.cfg")
        with open(existing, "wb") as f:
            f.write(b"OLD")
        local = os.path.join(tempfile.mkdtemp(), "config.cfg")
        with open(local, "wb") as f:
            f.write(b"NEW")
        errors, done = [], []
        m.error_occurred.connect(errors.append)
        m.transfer_complete.connect(done.append)

        m.upload_file(local, "/config.cfg")
        self.assertTrue(self._wait(lambda: errors or done), "完了もエラーも届かない")

        with open(existing, "rb") as f:
            self.assertEqual(f.read(), b"OLD", "確認なしで既存ファイルが潰された")
        self.assertEqual(done, [])
        self.assertTrue(any("既にあります" in e for e in errors), errors)

        # 承認済みなら置き換わる
        m.upload_file(local, "/config.cfg", overwrite=True)
        self.assertTrue(self._wait(lambda: done), "承認済みの上書きが完了しない")
        with open(existing, "rb") as f:
            self.assertEqual(f.read(), b"NEW")


if __name__ == "__main__":
    unittest.main()
