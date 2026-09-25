"""内蔵 FTP サーバーへ同じ保存先を並行して STOR すると、中身が混ざる・片方が
黙って消える件（srv-01 の FTP 版）。

何が起きていたか（実測、基準 028ebc2。127.0.0.1 のみ）: ftplib で認証済みの
2 接続から TYPE I で、

  1) A が STOR config.cfg を始めて b"AAAA" を送る
  2) B が同じ名前へ STOR して b"B" * 20 を送り、閉じる
  3) A が b"aaaaaaaa" を送って閉じる

とすると、A も B も '226 Transfer complete.' で終わるのに、残ったファイルは
b"AAAAaaaaaaaaBBBBBBBB" だった（B が 8 バイトなら b"AAAAaaaaaaaa" で、B の
アップロードは黙って消えた）。pyftpdlib の STOR はそれぞれが保存先を 'wb' で
開くだけで、同じ保存先を断る仕組みが無い。SFTP（srv-01）と TFTP（同名の
WRQ）は既に断っている。匿名の書き込みを有効にした構成では認証なしで起こせる。

どう直したか: _Handler.ftp_STOR（APPE も REST 付きの STOR もここを通る）で、
pyftpdlib が開く前に保存先（実パスを realpath → normcase した鍵）を
マネージャ側へ予約する。別の接続が予約していれば 450 で断り、パネルのログへ
相手の IP 付きで理由を出す。予約は受信の完了（on_file_received）・未完了
（on_incomplete_file_received）・STOR の失敗（550 / 554）で外し、制御接続が
閉じたときにも外す（データ接続が来ないまま STOR だけ受けて相手が去ると、
どのコールバックも呼ばれない。受動ポートがファイアウォールで塞がれている
ときに起きる）。読み取り（RETR）と別の保存先への STOR は妨げない。
"""
import ftplib
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

USER = "tester"
PASSWORD = "example-pass"


class _FtpServerCase(unittest.TestCase):
    """FTP サーバーを 127.0.0.1 で起動して、ftplib で操作する土台（テストは持たない）"""
    anonymous = False

    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        self.root = tempfile.mkdtemp(prefix="netbelt-ftp-samepath-")
        from core.ftp_server import FTPServerManager
        self.m = FTPServerManager()
        # 無人テストで実ファイアウォールを叩かないようスタブする
        fw = mock.patch("core.firewall.ensure_inbound_allow",
                        return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)
        self.activity = []
        self.m.client_activity.connect(
            lambda ip, message: self.activity.append((ip, message)))
        # テストのあとに届く知らせが、GC で空にされた lambda を呼ばないよう外す
        self.addCleanup(self.m.client_activity.disconnect)
        if self.anonymous:
            started = self.m.start(port=0, root_dir=self.root,
                                   anonymous=True, anonymous_write=True)
        else:
            started = self.m.start(port=0, root_dir=self.root,
                                   username=USER, password=PASSWORD)
        self.assertTrue(started)
        self.addCleanup(self.m.stop)

    def client(self):
        ftp = ftplib.FTP()
        ftp.connect("127.0.0.1", self.m.port, timeout=10)
        if self.anonymous:
            ftp.login()
        else:
            ftp.login(USER, PASSWORD)
        ftp.voidcmd("TYPE I")
        self.addCleanup(self._close, ftp)
        return ftp

    @staticmethod
    def _close(ftp):
        try:
            ftp.close()
        except Exception:
            pass

    def real(self, name):
        return os.path.join(self.root, name)

    def read(self, name):
        with open(self.real(name), "rb") as handle:
            return handle.read()

    def start_upload(self, ftp, name, first=b"AAAA"):
        """STOR を始めて first を送った状態のデータ接続を返す（閉じない）"""
        data = ftp.transfercmd("STOR " + name)
        self.addCleanup(self._close_socket, data)
        data.sendall(first)
        return data

    @staticmethod
    def _close_socket(sock):
        try:
            sock.close()
        except OSError:
            pass

    def finish_upload(self, ftp, data, rest=b"aaaaaaaa"):
        data.sendall(rest)
        data.close()
        return ftp.voidresp()

    def upload(self, ftp, name, payload):
        """1 本のアップロードを最後まで行い、最終応答を返す"""
        data = ftp.transfercmd("STOR " + name)
        data.sendall(payload)
        data.close()
        return ftp.voidresp()

    def upload_eventually(self, ftp, name, payload, seconds=5.0):
        """予約が外れるのを待ちながら、通るまでアップロードを試す"""
        deadline = time.monotonic() + seconds
        while True:
            try:
                return self.upload(ftp, name, payload)
            except ftplib.error_temp:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

    def _wait_activity(self, *needles, timeout=5.0):
        """パネルへ渡るログ（別スレッドから届く）を待って、該当する行を返す"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            for ip, message in self.activity:
                if all(needle in message for needle in needles):
                    return ip, message
            time.sleep(0.02)
        return None

    def check_a_second_stor_is_refused(self):
        """A が書いている間の B の STOR を 450 で断り、A の中身を守ること"""
        a, b = self.client(), self.client()
        data = self.start_upload(a, "config.cfg")
        with self.assertRaises(ftplib.error_temp,
                               msg="書き込み中の保存先へ別の接続が STOR できている") as caught:
            self.upload(b, "config.cfg", b"B" * 20)
        self.assertTrue(str(caught.exception).startswith("450"),
                        str(caught.exception))
        self.assertTrue(self.finish_upload(a, data).startswith("226"))
        self.assertEqual(self.read("config.cfg"), b"AAAAaaaaaaaa",
                         "先に書いていた側の中身が壊れている")

    def check_the_refusal_is_logged_with_the_client(self):
        """断った理由を、相手の IP と一緒にパネルのログへ日本語で出すこと"""
        a, b = self.client(), self.client()
        data = self.start_upload(a, "logged.cfg")
        with self.assertRaises(ftplib.error_temp):
            self.upload(b, "logged.cfg", b"B")
        self.finish_upload(a, data)
        found = self._wait_activity("書き込み中", "logged.cfg")
        self.assertIsNotNone(found, "断った理由がログに出ていない: %r"
                             % (self.activity,))
        self.assertEqual(found[0], "127.0.0.1")


class FtpConcurrentStorRefusedTest(_FtpServerCase):
    def test_a_second_stor_is_refused_and_the_content_is_not_mixed(self):
        """先に書いている相手がいる間、同じ保存先への STOR を断り、中身を混ぜないこと。"""
        self.check_a_second_stor_is_refused()

    def test_appe_to_a_target_being_written_is_refused(self):
        """APPE（追記）も、別の接続が書き込み中なら断ること。"""
        a, b = self.client(), self.client()
        data = self.start_upload(a, "append.cfg")
        with self.assertRaises(ftplib.error_temp):
            conn = b.transfercmd("APPE append.cfg")
            self._close_socket(conn)
        self.finish_upload(a, data)
        self.assertEqual(self.read("append.cfg"), b"AAAAaaaaaaaa")

    def test_the_refusal_is_logged_with_the_client(self):
        """断った理由を、どの相手かと一緒にパネルのログへ日本語で出すこと。"""
        self.check_the_refusal_is_logged_with_the_client()

    def test_case_differences_are_the_same_target(self):
        """大文字小文字・パスの書き方だけが違う名前も、同じ保存先として断ること。"""
        os.makedirs(self.real("dir"), exist_ok=True)
        a, b = self.client(), self.client()
        data = self.start_upload(a, "/dir/Case.cfg")
        for other in ("dir/case.CFG", "/DIR/CASE.cfg", "dir/./case.cfg"):
            with self.subTest(name=other):
                with self.assertRaises(ftplib.error_temp,
                                       msg="%r が同じ保存先として扱われていない" % other):
                    self.upload(b, other, b"B")
        self.finish_upload(a, data)
        self.assertEqual(self.read(os.path.join("dir", "Case.cfg")),
                         b"AAAAaaaaaaaa")

    def test_the_target_is_free_again_once_the_first_upload_completes(self):
        """先のアップロードが終わったら、次の STOR を受け付けること。"""
        a, b = self.client(), self.client()
        self.assertTrue(self.upload(a, "again.cfg", b"first").startswith("226"))
        self.assertTrue(self.upload(b, "again.cfg", b"second").startswith("226"))
        self.assertEqual(self.read("again.cfg"), b"second")

    def test_an_upload_cut_off_midway_frees_the_target(self):
        """途中で相手が切れた（未完了の）アップロードは、予約を残さないこと。"""
        a, b = self.client(), self.client()
        data = self.start_upload(a, "cut.cfg")
        a.close()               # 制御接続ごと切る（QUIT も ABOR も送らない）
        data.close()
        self.assertTrue(self.upload_eventually(b, "cut.cfg", b"retry")
                        .startswith("226"))
        self.assertEqual(self.read("cut.cfg"), b"retry")

    def test_an_aborted_upload_frees_the_target(self):
        """ABOR で止めたアップロードは、制御接続が残っていても予約を残さないこと。"""
        a, b = self.client(), self.client()
        self.start_upload(a, "abort.cfg")
        a.putcmd("ABOR")
        # 受信済みのバイトがあれば 426 のあとに 226、まだ無ければ 225 だけが返る
        reply = a.getline()
        while reply[:3] not in ("225", "226"):
            self.assertTrue(reply.startswith("426"), reply)
            reply = a.getline()
        self.assertTrue(self.upload(b, "abort.cfg", b"after").startswith("226"),
                        "ABOR した相手の予約が外れない")
        self.assertEqual(self.read("abort.cfg"), b"after")

    def test_a_stor_whose_data_connection_never_came_frees_the_target(self):
        """STOR を受けたがデータ接続が来ないまま相手が去っても、予約を残さないこと。"""
        a, b = self.client(), self.client()
        a.sendcmd("PASV")
        self.assertTrue(a.sendcmd("STOR queued.cfg").startswith("150"))
        with self.assertRaises(ftplib.error_temp,
                               msg="データ接続を待っている STOR の保存先が予約されていない"):
            self.upload(b, "queued.cfg", b"B")
        a.close()               # QUIT を送らずに切る
        self.assertTrue(self.upload_eventually(b, "queued.cfg", b"after")
                        .startswith("226"),
                        "去った相手の予約が外れない")
        self.assertEqual(self.read("queued.cfg"), b"after")

    def test_a_failed_stor_does_not_keep_the_target_reserved(self):
        """開けなかった STOR（REST の位置が大きさを超える）は、予約を残さないこと。"""
        with open(self.real("exists.cfg"), "wb") as seed:
            seed.write(b"OLD")
        a, b = self.client(), self.client()
        a.sendcmd("REST 100")
        with self.assertRaises(ftplib.error_perm):
            conn = a.transfercmd("STOR exists.cfg")
            self._close_socket(conn)
        self.assertTrue(self.upload(b, "exists.cfg", b"NEW").startswith("226"))
        self.assertEqual(self.read("exists.cfg"), b"NEW")

    def test_uploads_to_different_targets_are_not_blocked(self):
        """別の保存先への並行アップロードは、これまでどおり通ること。"""
        a, b = self.client(), self.client()
        one = self.start_upload(a, "one.cfg", b"one")
        two = self.start_upload(b, "two.cfg", b"two")
        self.assertTrue(self.finish_upload(a, one, b"").startswith("226"))
        self.assertTrue(self.finish_upload(b, two, b"").startswith("226"))
        self.assertEqual(self.read("one.cfg"), b"one")
        self.assertEqual(self.read("two.cfg"), b"two")

    def test_reading_is_not_blocked_while_another_client_writes(self):
        """書き込み中の保存先でも、RETR は妨げないこと。"""
        a, b = self.client(), self.client()
        data = self.start_upload(a, "read.cfg")
        got = []
        self.assertTrue(b.retrbinary("RETR read.cfg", got.append).startswith("226"))
        self.assertIn(b"".join(got), (b"", b"AAAA"))
        self.finish_upload(a, data)
        self.assertEqual(self.read("read.cfg"), b"AAAAaaaaaaaa")


class FtpConcurrentStorRefusedAnonymousTest(_FtpServerCase):
    """匿名の書き込みを有効にした構成でも同じく断ること（認証なしで起こせるため）。"""
    anonymous = True

    def test_a_second_anonymous_stor_is_refused(self):
        """匿名の 2 接続でも、同じ保存先への STOR を断って中身を守ること。"""
        self.check_a_second_stor_is_refused()

    def test_the_anonymous_refusal_is_logged(self):
        """匿名の接続を断ったときも、理由をパネルのログへ出すこと。"""
        self.check_the_refusal_is_logged_with_the_client()


if __name__ == "__main__":
    unittest.main()
