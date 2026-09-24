"""同じ保存先の STOR を 450 で断ったとき、REST の再開位置を残さないことを検証する。

何が起きていたか（実測、基準 38e7b29。127.0.0.1 のみ）。da8f5ea で、
別の接続が書き込み中の保存先への STOR を、pyftpdlib が開く前に 450 で
断るようにした。断る分岐は super().ftp_STOR を呼ばずに戻るので、
pyftpdlib の ftp_STOR の先頭にある「REST の位置を読んで 0 に戻す」を
通らない。そのため、

  1) A が busy.cfg へ STOR して書いている
  2) B が REST 4 → STOR busy.cfg → 450 で断られる
  3) B が REST 無しで、既存の fresh.cfg（b"0123456789"）へ b"XY" を STOR する

と、3) は 226 で終わるのに fresh.cfg は b"0123XY6789" になった（残っていた
位置 4 から上書きされた）。3) を RETR にすると、先頭 4 バイトを飛ばした
b"456789" しか返らなかった。REST を使って再送する相手が 450 を受けたあと、
成功応答のまま中身が壊れる。pyftpdlib 単体（550 で断る経路など）では、
REST は STOR / RETR のたびに消費されるので起きない。

どう直したか: 断る分岐でも pyftpdlib と同じく _restart_position を 0 に
戻してから 450 を返す。
"""
import ftplib
import os
import sys
import unittest

sys.path.insert(0, "tests")

from test_ftp_server_concurrent_stor_refused import _FtpServerCase  # noqa: E402


class RefusedStorClearsRestTest(_FtpServerCase):
    def _refused_after_rest(self, b):
        """B に REST 4 を送ってから、書き込み中の busy.cfg への STOR を断らせる"""
        a = self.client()
        data = self.start_upload(a, "busy.cfg")
        self.addCleanup(self.finish_upload_quietly, a, data)
        b.sendcmd("REST 4")
        with self.assertRaises(ftplib.error_temp):
            b.transfercmd("STOR busy.cfg")

    def finish_upload_quietly(self, ftp, data):
        try:
            self.finish_upload(ftp, data)
        except (ftplib.Error, OSError):
            pass

    def _put(self, name, payload):
        with open(self.real(name), "wb") as handle:
            handle.write(payload)

    def test_next_stor_after_refusal_starts_at_the_beginning(self):
        """断られたあとの REST 無しの STOR が、残った位置から書かないこと。"""
        self._put("fresh.cfg", b"0123456789")
        b = self.client()
        self._refused_after_rest(b)

        self.upload(b, "fresh.cfg", b"XY")

        self.assertEqual(self.read("fresh.cfg"), b"XY",
                         "断った STOR の REST が残り、途中から上書きした")

    def test_next_retr_after_refusal_returns_the_whole_file(self):
        """断られたあとの REST 無しの RETR が、先頭から全部返すこと。"""
        self._put("get.cfg", b"0123456789")
        b = self.client()
        self._refused_after_rest(b)

        got = []
        b.retrbinary("RETR get.cfg", got.append)

        self.assertEqual(b"".join(got), b"0123456789",
                         "断った STOR の REST が残り、途中から返した")

    def test_rest_still_works_for_the_next_stor_that_asks_for_it(self):
        """断られたあとでも、REST を付け直した STOR はその位置から書くこと（対照）。

        pyftpdlib は REST 付きの STOR で後ろを切り詰めず、その位置から上書き
        する（既存の挙動。この修正とは関係ない）。
        """
        self._put("resume.cfg", b"0123456789")
        b = self.client()
        self._refused_after_rest(b)

        b.sendcmd("REST 6")
        data = b.transfercmd("STOR resume.cfg")
        data.sendall(b"XY")
        data.close()
        b.voidresp()

        self.assertEqual(self.read("resume.cfg"), b"012345XY89")


if __name__ == "__main__":
    unittest.main()
