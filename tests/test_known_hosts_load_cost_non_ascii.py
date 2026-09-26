"""ASCII 以外を含む known_hosts でも、ハッシュ化行があるときの読み込みが 2 乗で重くならないことを検証する。

何が起きていたか（a18138e で実測）。ハッシュ化名の行（|1|salt|hash）がある
ファイルは、読み込みの 2 乗の重さを避けて自前のローダで読むようにしたが、
自前のローダは UTF-8 で読むので、日本語のホスト名（NetBelt は日本語の
Windows では cp932 で保存する）が文字化けして、その機器が「未知」に戻る。
そのため f7a8590 で、ASCII 以外を含むファイルは今までどおり paramiko の
HostKeys.load に読ませていた。HostKeys.load は 1 行ごとに check() → lookup で、
それまでに読んだハッシュ化名すべてに hash_host を掛け直すので、

    半分ハッシュ化の 2,000 行＋cp932 の日本語ホスト名の行 1 行 : 506,500 回・8.6 秒
    同じ 2,000 行＋UTF-8 の日本語コメント 1 行                 : 495,555 回・7.4 秒

と、ASCII 以外が 1 バイトあるだけで重さが元に戻っていた。この読み込みは
接続スレッドで、しかも known_hosts の錠の中で動くので、同時に繋ぐ接続や
鍵の保存がその間ずっと待つ。

どう直したか。ハッシュ化名の行があるファイルは、paramiko の HostKeys.load
そのもの（既定の文字コード・text モードの読み方）で読み、1 行ごとの重複の
判定（check）だけを名前の文字列の比較に替えた HostKeys へ読み込む。読み方は
paramiko と同じなので、cp932 の名前も今までどおり読める。既定の文字コードで
読めないファイルは、今までどおり自前のローダへ回す。

テストの前提: paramiko.hostkeys の open を、文字コードを指定しないテキストを
cp932 で開くものに差し替える（日本語の Windows の既定と同じ）。どの環境で
流しても同じ読み方になる。呼び出し回数で見るので、CPU の混雑で結果は揺れない。
"""
import builtins
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

import paramiko                                     # noqa: E402
from paramiko.hostkeys import HostKeys              # noqa: E402

LINES = 400
JP_HOST = "東京-sw"


def _host(i):
    return "198.51.100.%d" % (i % 250 + 1) if i < 250 else \
        "203.0.113.%d" % (i - 250 + 1)


def _open_with_cp932_default(file, mode="r", buffering=-1, encoding=None,
                             *args, **kwargs):
    """文字コードを指定しないテキストの open を、日本語の Windows と同じ cp932 で開く"""
    if "b" not in mode and encoding is None:
        encoding = "cp932"
    return builtins.open(file, mode, buffering, encoding, *args, **kwargs)


class KnownHostsLoadCostNonAsciiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_jp = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khcost-jp-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        for patcher in (
                mock.patch("core.config_manager.app_data_dir",
                           return_value=self.dir),
                mock.patch.object(paramiko.hostkeys, "open",
                                  _open_with_cp932_default, create=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.known_hosts = self.dir / "known_hosts"

    def _line(self, name, key):
        return "%s %s %s\n" % (name, key.get_name(), key.get_base64())

    def _layouts(self):
        """平文とハッシュ化を交互に LINES 行と、ASCII 以外を含む行の組み合わせ。"""
        alternating = "".join(
            self._line(HostKeys.hash_host(_host(i)) if i % 2 == 0 else _host(i),
                       self.key_a)
            for i in range(LINES)).encode("ascii")
        jp_line = self._line(JP_HOST, self.key_jp).encode("cp932")
        # cp932 では読めない（UTF-8 の「東京ラボの機器」）。末尾に置くと、
        # paramiko は 2 乗の読み込みを最後まで済ませてから読めずに落ちる
        comment = "# 東京ラボの機器\n".encode("utf-8")
        return {
            "cp932 host name at the end": alternating + jp_line,
            "cp932 host name at the top": jp_line + alternating,
            "UTF-8 comment at the end": alternating + comment,
        }

    def _setup_host_keys(self):
        """本物の SSHClient に _setup_host_keys を通し、その client を返す。"""
        from core.ssh_connection import SSHConnection
        conn = SSHConnection("192.0.2.1", 22, "admin", password="pw")
        self.addCleanup(conn.deleteLater)
        client = paramiko.SSHClient()
        self.addCleanup(client.close)
        conn._setup_host_keys(client)
        return client

    def test_utf8_comment_is_not_cp932(self):
        """前提: UTF-8 のコメントの組み合わせは、cp932 では読めないこと。"""
        with self.assertRaises(UnicodeDecodeError):
            self._layouts()["UTF-8 comment at the end"].decode("cp932")

    def test_hash_host_calls_grow_linearly(self):
        """ASCII 以外を含んでも、hash_host の呼び出しが行数の 4 倍以下であること。"""
        real_hash_host = HostKeys.hash_host
        for title, data in self._layouts().items():
            with self.subTest(title):
                self.known_hosts.write_bytes(data)
                calls = []

                def counting(hostname, salt=None):
                    calls.append(hostname)
                    return real_hash_host(hostname, salt)

                with mock.patch.object(HostKeys, "hash_host",
                                       staticmethod(counting)):
                    self._setup_host_keys()

                self.assertLessEqual(
                    len(calls), LINES * 4,
                    "known_hosts %d 行の読み込みで hash_host を %d 回呼んでいる"
                    "（行数の 2 乗で増える読み方をしている）"
                    % (LINES, len(calls)))

    def test_every_line_is_still_looked_up(self):
        """先頭・中ほど・末尾の接続先と日本語のホスト名の鍵が、今までどおり引けること。"""
        for title, data in self._layouts().items():
            with self.subTest(title):
                self.known_hosts.write_bytes(data)
                keys = self._setup_host_keys().get_host_keys()

                for i in (0, 1, LINES // 2, LINES // 2 + 1,
                          LINES - 2, LINES - 1):
                    found = keys.lookup(_host(i))
                    self.assertIsNotNone(
                        found, "%d 行目の %s が引けない" % (i + 1, _host(i)))
                    self.assertEqual(found[self.key_a.get_name()], self.key_a)
                if "cp932" in title:
                    found = keys.lookup(JP_HOST)
                    self.assertIsNotNone(found, "日本語のホスト名が引けない")
                    self.assertEqual(found[self.key_jp.get_name()],
                                     self.key_jp)
                self.assertIsNone(keys.lookup("192.0.2.200"))


if __name__ == "__main__":
    unittest.main()
