# -*- coding: utf-8 -*-
"""FTP で、Windows が別の実体に化かす名前を 550 で断ることを検証する。

pyftpdlib のルート拘束（AbstractedFS.validpath）は realpath と接頭辞の比較で、
NUL のような予約デバイス名、`name:stream` の代替データストリーム、末尾の
ドットや空白の名前を「ルート配下のパス」として通していた。匿名の書き込みでも
STOR NUL は 226 なのに何も保存されず、STOR exists.txt:ads は既存ファイルに
一覧へ出ないストリームを足し、STOR 'config. ' は config として保存された（実測）。
"""
import ftplib
import io
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, "src")


class FtpUnsafeNamesTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="netbelt-ftp-unsafe-")
        from core.ftp_server import FTPServerManager
        self.m = FTPServerManager()
        patch = unittest.mock.patch(
            "core.firewall.ensure_inbound_allow", return_value=(True, "test stub"))
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.m.stop)
        with open(os.path.join(self.root, "exists.txt"), "wb") as fh:
            fh.write(b"ORIGINAL")

    def _login(self, anonymous=True):
        if anonymous:
            ok = self.m.start(port=0, root_dir=self.root, username="", password="",
                              anonymous=True, anonymous_write=True)
        else:
            ok = self.m.start(port=0, root_dir=self.root, username="user",
                              password="secret")
        self.assertTrue(ok)
        time.sleep(0.3)
        f = ftplib.FTP()
        f.connect("127.0.0.1", self.m.port, timeout=5)
        if anonymous:
            f.login()
        else:
            f.login("user", "secret")
        self.addCleanup(f.close)
        return f

    def _assert_550(self, func, *args):
        with self.assertRaises(ftplib.error_perm) as cm:
            func(*args)
        self.assertTrue(str(cm.exception).startswith("550"), str(cm.exception))

    def _listing(self):
        return sorted(os.listdir(self.root))

    def test_anonymous_store_to_a_device_name_is_refused(self):
        f = self._login()
        for name in ("NUL", "nul", "COM1", "NUL.txt", "sub/NUL"):
            with self.subTest(name=name):
                self._assert_550(f.storbinary, "STOR %s" % name, io.BytesIO(b"x"))
        self.assertEqual(self._listing(), ["exists.txt"])

    def test_anonymous_store_to_an_alternate_data_stream_is_refused(self):
        f = self._login()
        self._assert_550(f.storbinary, "STOR exists.txt:ads", io.BytesIO(b"hidden"))
        self.assertFalse(os.path.exists(os.path.join(self.root, "exists.txt:ads")),
                         "既存ファイルに代替データストリームが足された")
        with open(os.path.join(self.root, "exists.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"ORIGINAL")

    def test_anonymous_store_with_a_trailing_dot_or_space_is_refused(self):
        f = self._login()
        for name in ("config. ", "config.", "config "):
            with self.subTest(name=name):
                self._assert_550(f.storbinary, "STOR %s" % name, io.BytesIO(b"x"))
        self.assertEqual(self._listing(), ["exists.txt"])

    def test_anonymous_append_and_unique_store_are_refused(self):
        f = self._login()
        self._assert_550(f.storbinary, "APPE NUL", io.BytesIO(b"x"))
        self._assert_550(f.storbinary, "APPE exists.txt:ads", io.BytesIO(b"x"))
        self._assert_550(f.storbinary, "STOU NUL", io.BytesIO(b"x"))
        self._assert_550(f.storbinary, "STOU exists.txt:ads", io.BytesIO(b"x"))
        self.assertEqual(self._listing(), ["exists.txt"])

    def test_anonymous_retrieve_from_a_device_name_is_refused(self):
        f = self._login()
        self._assert_550(f.retrbinary, "RETR NUL", lambda _data: None)
        self._assert_550(f.retrbinary, "RETR exists.txt:$DATA", lambda _data: None)

    def test_user_commands_with_unsafe_names_are_refused(self):
        f = self._login(anonymous=False)
        f.sendcmd("TYPE I")   # SIZE は ASCII モードでは常に 550
        self._assert_550(f.delete, "NUL")
        self._assert_550(f.mkd, "newdir.")
        self._assert_550(f.mkd, "CON")
        self._assert_550(f.rmd, "AUX")
        self._assert_550(f.sendcmd, "SIZE NUL")
        self._assert_550(f.sendcmd, "MDTM exists.txt:ads")
        self._assert_550(f.sendcmd, "RNFR exists.txt:ads")
        f.sendcmd("RNFR exists.txt")
        self._assert_550(f.sendcmd, "RNTO renamed.txt ")
        self.assertEqual(self._listing(), ["exists.txt"])

    def test_the_refusal_is_logged_with_the_name(self):
        """断った理由と名前がアクティビティログに出ること（機器側の失敗の手がかり）。"""
        seen = []
        self.m.client_activity.connect(lambda ip, msg: seen.append(msg))
        f = self._login()
        self._assert_550(f.storbinary, "STOR NUL", io.BytesIO(b"x"))
        deadline = time.time() + 2
        while time.time() < deadline and not any("/NUL" in m for m in seen):
            self.app.processEvents()
            time.sleep(0.02)
        self.assertTrue(any("/NUL" in m and "予約デバイス名" in m for m in seen), seen)

    def test_ordinary_names_still_work(self):
        f = self._login()
        f.storbinary("STOR running-config", io.BytesIO(b"hostname R1"))
        f.storbinary("STOR c9300.bin", io.BytesIO(b"bin"))
        resp = f.storbinary("STOU", io.BytesIO(b"unique"))
        self.assertTrue(resp.startswith("226"), resp)
        buf = io.BytesIO()
        f.retrbinary("RETR running-config", buf.write)
        self.assertEqual(buf.getvalue(), b"hostname R1")
        self.assertIn("exists.txt", f.nlst())
        f.sendcmd("TYPE I")
        self.assertTrue(f.sendcmd("SIZE exists.txt").startswith("213"))


if __name__ == "__main__":
    unittest.main()
