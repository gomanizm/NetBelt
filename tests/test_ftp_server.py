"""FTPServerManager (pyftpdlib wrapper) の STOR/RETR 統合テスト。"""
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
import ftplib
import io

sys.path.insert(0, "src")


class FtpServerTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        self.root = tempfile.mkdtemp()
        from core.ftp_server import FTPServerManager
        self.m = FTPServerManager()
        # 無人テストで実ファイアウォール（UAC/ルール追加）を叩かないようスタブ必須。
        # start() は core.firewall.ensure_inbound_allow を遅延importするため、
        # モジュール属性を直接パッチすれば制御/passive の両呼び出しに効く。
        self._fw_patch = unittest.mock.patch(
            "core.firewall.ensure_inbound_allow", return_value=(True, "test stub"))
        self._fw_patch.start()
        self.addCleanup(self._fw_patch.stop)
        self.assertTrue(self.m.start(port=0, root_dir=self.root,
                                      username="u", password="p"))
        self.port = self.m.port
        time.sleep(0.3)

    def tearDown(self):
        self.m.stop()

    def test_stor_and_retr(self):
        f = ftplib.FTP()
        f.connect("127.0.0.1", self.port, timeout=5)
        f.login("u", "p")
        f.storbinary("STOR run.cfg", io.BytesIO(b"hostname R1"))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "run.cfg")))
        buf = io.BytesIO()
        f.retrbinary("RETR run.cfg", buf.write)
        self.assertEqual(buf.getvalue(), b"hostname R1")
        f.quit()

    def test_transfer_complete_signal(self):
        got = []
        self.m.transfer_complete.connect(lambda ip, fn, done, total, d: got.append((fn, done, total, d)))
        import ftplib, io
        f = ftplib.FTP(); f.connect("127.0.0.1", self.port, timeout=5); f.login("u", "p")
        f.storbinary("STOR run.cfg", io.BytesIO(b"hostname R1"))
        buf = io.BytesIO(); f.retrbinary("RETR run.cfg", buf.write); f.quit()
        import time; time.sleep(0.4)
        # Qt キューを回す
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        self.assertTrue(any(g[0].endswith("run.cfg") and g[3] == "upload" for g in got))
        self.assertTrue(any(g[3] == "download" for g in got))


    def test_start_does_not_touch_firewall(self):
        # 3CDaemon 方式: 起動時に FW 自動設定（UAC 昇格経路）を呼ばないこと。
        from unittest import mock
        import core.firewall as fw
        from core.ftp_server import FTPServerManager
        with mock.patch.object(fw, "ensure_self_program_allow") as m_self:
            with mock.patch.object(fw, "ensure_inbound_allow") as m_in:
                m2 = FTPServerManager()
                try:
                    self.assertTrue(m2.start(port=0, root_dir=self.root,
                                             username="u2", password="p2"))
                    m_self.assert_not_called()
                    m_in.assert_not_called()
                finally:
                    m2.stop()


    def test_fix_firewall_invokes_helpers(self):
        # 手動FW許可ボタンの中身: 制御+passiveで ensure_inbound_allow を2回、自exeで1回。
        from unittest import mock
        import core.firewall as fw
        from core.ftp_server import FTPServerManager
        with mock.patch.object(fw, "ensure_inbound_allow", return_value=(True, "ok")) as m_in:
            with mock.patch.object(fw, "ensure_self_program_allow", return_value=(True, "ok")) as m_self:
                m = FTPServerManager()
                ok, _ = m.fix_firewall(21, (50100, 50150))
                self.assertTrue(ok)
                self.assertEqual(m_in.call_count, 2)   # 制御 + passive
                m_self.assert_called_once()


    def test_transfer_emits_started_progress_complete(self):
        # TFTPと同じ見た目にするため、FTPも開始/進捗/完了を発火すること（両方向）。
        started, progress, complete = [], [], []
        self.m.transfer_started.connect(lambda ip, fn, t, d: started.append(d))
        self.m.transfer_progress.connect(lambda ip, fn, done, t, d: progress.append(done))
        self.m.transfer_complete.connect(lambda ip, fn, done, t, d: complete.append(d))
        import ftplib, io, time
        payload = b"x" * (512 * 1024)  # 進捗が最低1回は出るサイズ
        f = ftplib.FTP(); f.connect("127.0.0.1", self.port, timeout=5); f.login("u", "p")
        f.storbinary("STOR big.bin", io.BytesIO(payload))       # アップロード(STOR)
        buf = io.BytesIO(); f.retrbinary("RETR big.bin", buf.write)  # ダウンロード(RETR)
        f.quit()
        time.sleep(0.5)
        from PyQt6.QtWidgets import QApplication; QApplication.processEvents()
        self.assertIn("upload", started)      # STORで転送開始が発火
        self.assertIn("download", started)    # RETRで転送開始が発火
        self.assertGreaterEqual(len(progress), 1)  # 進捗が発火
        self.assertIn("upload", complete)     # 完了(両方向)
        self.assertIn("download", complete)


    def test_manager_coalesces_multiple_started_same_key(self):
        # 機器が同一ファイルで複数FTP接続を張っても履歴は1行（transfer_started 1回に束ねる）。
        started = []
        self.m.transfer_started.connect(lambda ip, fn, t, d: started.append((ip, fn, d)))
        for _ in range(4):
            self.m._emit_started("192.0.2.31", "x.exe", 100, "download")
        from PyQt6.QtWidgets import QApplication; QApplication.processEvents()
        self.assertEqual(len(started), 1)          # 4接続を1行に束ねる
        self.m._emit_complete("192.0.2.31", "x.exe", 100, 100, "download")
        self.m._emit_started("192.0.2.31", "x.exe", 100, "download")
        QApplication.processEvents()
        self.assertEqual(len(started), 2)          # 完了後の再転送は新規行


class FtpServerAnonymousTest(unittest.TestCase):
    """匿名のみ（ユーザー名・パスワード未入力）でも起動し、匿名ログインでSTOR/RETRできることを検証。"""

    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        self.root = tempfile.mkdtemp()
        from core.ftp_server import FTPServerManager
        self.m = FTPServerManager()
        # 無人テストで実ファイアウォール（UAC/ルール追加）を叩かないようスタブ必須。
        self._fw_patch = unittest.mock.patch(
            "core.firewall.ensure_inbound_allow", return_value=(True, "test stub"))
        self._fw_patch.start()
        self.addCleanup(self._fw_patch.stop)
        # username/password を空のまま、anonymous=True で起動できることが本件の検証対象。
        self.assertTrue(self.m.start(port=0, root_dir=self.root,
                                      username="", password="", anonymous=True))
        self.assertTrue(self.m.is_running)
        self.port = self.m.port
        time.sleep(0.3)

    def tearDown(self):
        self.m.stop()

    def test_anonymous_stor_and_retr(self):
        f = ftplib.FTP()
        f.connect("127.0.0.1", self.port, timeout=5)
        f.login()  # 引数なし = 匿名ログイン
        f.storbinary("STOR anon.cfg", io.BytesIO(b"hostname R2"))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "anon.cfg")))
        buf = io.BytesIO()
        f.retrbinary("RETR anon.cfg", buf.write)
        self.assertEqual(buf.getvalue(), b"hostname R2")
        f.quit()


if __name__ == "__main__":
    unittest.main()
