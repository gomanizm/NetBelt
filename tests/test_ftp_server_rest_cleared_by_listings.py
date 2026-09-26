"""REST の後の一覧（LIST / NLST / MLSD）でも、REST の位置を残さないこと。

何が起きていたか（実測、ebbe593。127.0.0.1 のみ）: REST 4 の後に LIST / NLST /
MLSD を送ると、一覧は普通に返るが REST の位置が残り、次の REST 無しの
RETR get.cfg（中身 b'0123456789'）が b'456789' を返した（先頭 4 バイトが欠けた
まま 226）。pyftpdlib 2.2.0 の一覧のコマンドは REST の位置を読まず、0 にも戻さない。
1.3.2 で、断った STOR / APPE / STOU / RETR の後は位置を戻すようにしたが
（tests/test_ftp_server_rest_cleared_by_refused_transfers.py）、一覧の
データ転送はその対象に入っていなかった。

どう直したか: _Handler.pre_process_command で位置を 0 に戻す対象に、データ
接続を使う一覧のコマンド（LIST / NLST / MLSD）を足した。REST はその直後の
転送コマンドにだけ効く（RFC 959）ので、一覧が REST を消費した後の転送は
先頭から始まる。REST と転送の間の PASV・TYPE などは今までどおり位置を消費
しない（REST → PASV → RETR と送る機器の再開は変わらない）。
"""
import ftplib
import shutil
import sys
import unittest

sys.path.insert(0, "tests")

from test_ftp_server_concurrent_stor_refused import _FtpServerCase  # noqa: E402

CONTENT = b"0123456789"


class ListingsClearRestTest(_FtpServerCase):
    def setUp(self):
        # サーバーの停止（super の後始末）より後に消すため、先に登録する
        self.addCleanup(self._remove_root)
        super().setUp()
        with open(self.real("get.cfg"), "wb") as seed:
            seed.write(CONTENT)

    def _remove_root(self):
        root = getattr(self, "root", None)
        if root:
            shutil.rmtree(root, ignore_errors=True)

    @staticmethod
    def _retr(ftp, name="get.cfg"):
        got = []
        ftp.retrbinary("RETR " + name, got.append)
        return b"".join(got)

    def _listings(self):
        return (("LIST", lambda f: f.retrlines("LIST", lambda line: None)),
                ("NLST", lambda f: f.nlst()),
                ("MLSD", lambda f: list(f.mlsd())))

    def test_retr_after_a_listing_returns_the_whole_file(self):
        for name, listing in self._listings():
            with self.subTest(listing=name):
                c = self.client()
                c.sendcmd("REST 4")
                listing(c)
                self.assertEqual(self._retr(c), CONTENT,
                                 "REST の後の %s が位置を残し、次の RETR が途中から返した"
                                 % name)

    def test_retr_after_a_refused_listing_returns_the_whole_file(self):
        c = self.client()
        c.sendcmd("REST 4")
        with self.assertRaises(ftplib.error_perm):
            c.retrlines("LIST missing-dir", lambda line: None)
        self.assertEqual(self._retr(c), CONTENT,
                         "断った LIST の後も REST の位置が残った")

    def test_rest_still_applies_to_the_transfer_right_after_it(self):
        """対照: 一覧の後に REST を付け直した RETR はその位置から始まる。
        REST と RETR の間の PASV（retrbinary が送る）は位置を消費しない"""
        c = self.client()
        c.sendcmd("REST 4")
        c.nlst()
        # 一覧で ASCII に切り替わっている（ASCII では REST を断られる）
        c.voidcmd("TYPE I")
        c.sendcmd("REST 4")
        self.assertEqual(self._retr(c), b"456789")
        self.assertEqual(self._retr(c), CONTENT)


if __name__ == "__main__":
    unittest.main()
