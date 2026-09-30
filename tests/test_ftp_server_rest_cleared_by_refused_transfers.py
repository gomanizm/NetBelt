"""権限の 550 や APPE / STOU の 450 で断った転送コマンドでも、REST の位置を残さないこと。

何が起きていたか（実測、基準 441ea02。127.0.0.1 のみ）:
  - REST 4 → APPE fresh.cfg は '450 Can't APPE while REST request is pending.'
    → REST 無しの STOR fresh.cfg b'XY' は 226 なのに中身は b'0123XY6789'
  - REST 4 → STOU は '450 Can't STOU while REST request is pending.'
    → REST 無しの RETR get.cfg は b'456789'（先頭 4 バイトが欠ける）
  - 匿名・読み取り専用で REST 4 → STOR up.cfg は '550 Not enough privileges.'
    → REST 無しの RETR get.cfg は b'456789'
pyftpdlib 2.2.0 で REST の位置を読んで 0 に戻すのは ftp_STOR と ftp_RETR の
先頭だけで、権限で断る 550（pre_process_command が ftp_* を呼ばずに戻る）と、
APPE / STOU の 450（ftp_STOR を通らずに戻る）は位置を残す。成功応答のまま、
次の REST 無しの転送が途中から書く・途中から返す。NetBelt が既に直していたのは
同時書き込みを断る自前の 450 だけ（tests/test_ftp_server_refused_stor_clears_rest.py）。

どう直したか: _Handler.pre_process_command を包み、転送コマンド（STOR / APPE /
STOU / RETR）を処理し終えたら、結果によらず _restart_position を 0 に戻す。
RFC 959 では REST はその直後の転送コマンドにだけ効くので、REST を付け直した
転送は従来どおりその位置から始まる。
"""
import ftplib
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "tests")

from test_ftp_server_concurrent_stor_refused import _FtpServerCase  # noqa: E402


class _RestHelpers:
    def _put(self, name, payload):
        with open(self.real(name), "wb") as handle:
            handle.write(payload)

    def _retr(self, ftp, name):
        got = []
        ftp.retrbinary("RETR " + name, got.append)
        return b"".join(got)

    def _refused(self, ftp, command, code):
        with self.assertRaises(ftplib.Error) as refused:
            ftp.transfercmd(command)
        self.assertTrue(str(refused.exception).startswith(code), str(refused.exception))


class RefusedAppeAndStouClearRestTest(_RestHelpers, _FtpServerCase):
    def test_stor_after_a_refused_appe_writes_from_the_beginning(self):
        self._put("fresh.cfg", b"0123456789")
        b = self.client()
        b.sendcmd("REST 4")
        self._refused(b, "APPE fresh.cfg", "450")
        self.assertTrue(self.upload(b, "fresh.cfg", b"XY").startswith("226"))
        self.assertEqual(self.read("fresh.cfg"), b"XY",
                         "断った APPE の REST が残り、途中から上書きした")

    def test_retr_after_a_refused_stou_returns_the_whole_file(self):
        self._put("get.cfg", b"0123456789")
        b = self.client()
        b.sendcmd("REST 4")
        self._refused(b, "STOU", "450")
        self.assertEqual(self._retr(b, "get.cfg"), b"0123456789",
                         "断った STOU の REST が残り、途中から返した")

    def test_rest_still_applies_to_the_transfer_right_after_it(self):
        """対照: REST を付け直した RETR / STOR はその位置から始まる"""
        self._put("get.cfg", b"0123456789")
        self._put("resume.cfg", b"0123456789")
        b = self.client()
        b.sendcmd("REST 4")
        self._refused(b, "APPE get.cfg", "450")
        b.sendcmd("REST 4")
        self.assertEqual(self._retr(b, "get.cfg"), b"456789")
        # 位置は 1 回で消費される
        self.assertEqual(self._retr(b, "get.cfg"), b"0123456789")
        b.sendcmd("REST 6")
        data = b.transfercmd("STOR resume.cfg")
        data.sendall(b"XY")
        data.close()
        b.voidresp()
        self.assertEqual(self.read("resume.cfg"), b"012345XY89")


class PermissionRefusalClearsRestTest(_RestHelpers, _FtpServerCase):
    """匿名・読み取り専用の構成（権限で断る 550 は pre_process_command で返る）"""
    anonymous = True    # client() が匿名で入る

    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        self.root = tempfile.mkdtemp(prefix="netbelt-ftp-rest-ro-")
        from core.ftp_server import FTPServerManager
        self.m = FTPServerManager()
        fw = mock.patch("core.firewall.ensure_inbound_allow",
                        return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)
        self.assertTrue(self.m.start(port=0, root_dir=self.root,
                                     anonymous=True, anonymous_write=False))
        self.addCleanup(self.m.stop)

    def test_retr_after_a_permission_refusal_returns_the_whole_file(self):
        self._put("get.cfg", b"0123456789")
        c = self.client()
        c.sendcmd("REST 4")
        self._refused(c, "STOR up.cfg", "550")
        self.assertEqual(self._retr(c, "get.cfg"), b"0123456789",
                         "権限で断った STOR の REST が残り、途中から返した")
        self.assertFalse(os.path.exists(self.real("up.cfg")))


if __name__ == "__main__":
    unittest.main()
