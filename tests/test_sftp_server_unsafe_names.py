# -*- coding: utf-8 -*-
"""内蔵 SFTP サーバーが、Windows で別の実体に化ける名前を断ることを検証する。

何が起きていたか: _get_real_path と _get_link_path は realpath と接頭辞の比較
だけでルート配下かを見ていた。NUL のような予約デバイス名は <root>\\NUL として
比較を通り、os.open がデバイスを開くので、アップロードは成功で終わるのに何も
残らなかった（一覧にも出ない）。まだ無い名前を data と data.（末尾のドット・
空白、data::$DATA）の別綴りで同時に書くと、realpath が畳まないので予約の鍵
（_OpenWriters._key）が 2 つに割れ、同じ実ファイルへ 2 本が書いて中身が壊れた。

どう直したか: realpath の前に core.safe_names.check_relative_path で各要素を
確かめ、化ける名前は IOError("Access denied") で断る。_get_link_path も同じ。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

UNSAFE = ["NUL", "nul", "CON", "COM1", "LPT1", "PRN", "AUX", "NUL.txt",
          "data.", "data ", "data::$DATA", "data:stream", "sub/NUL", "sub./x",
          "NUL/inner.txt"]
SAFE = ["data", "sub/data.txt", "設定.cfg", ".hidden", "a.b.c", "nullable"]


class SftpServerUnsafeNamesTest(unittest.TestCase):
    def setUp(self):
        from core.sftp_server import SFTPServerHandler, _OpenWriters
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-unsafe-")
        self.writers = _OpenWriters()
        self.handler = SFTPServerHandler(None, self.root, open_writers=self.writers)

    def test_unsafe_names_are_refused_by_get_real_path(self):
        for name in UNSAFE:
            for spelled in (name, "/" + name):
                with self.subTest(name=spelled):
                    with self.assertRaises(IOError):
                        self.handler._get_real_path(spelled)

    def test_unsafe_names_are_refused_by_get_link_path(self):
        for name in UNSAFE:
            with self.subTest(name=name):
                with self.assertRaises(IOError):
                    self.handler._get_link_path("/" + name)

    def test_plain_names_still_resolve_under_the_root(self):
        root_real = os.path.realpath(self.root)
        for name in SAFE:
            with self.subTest(name=name):
                real = self.handler._get_real_path("/" + name)
                self.assertTrue(real.startswith(os.path.join(root_real, "")))
                link = self.handler._get_link_path("/" + name)
                self.assertTrue(link.startswith(os.path.join(root_real, "")))
        self.assertEqual(self.handler._get_real_path("/"), root_real)

    def test_open_of_an_unsafe_name_does_not_reach_os_open(self):
        from paramiko import SFTP_FAILURE
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        calls = []
        real_open = os.open

        def recording_open(path, *args, **kwargs):
            calls.append(path)
            return real_open(path, *args, **kwargs)

        for name in ("NUL", "data.", "data::$DATA"):
            with self.subTest(name=name):
                calls.clear()
                with mock.patch("os.open", recording_open):
                    rc = self.handler.open("/" + name, flags, None)
                self.assertEqual(rc, SFTP_FAILURE)
                self.assertEqual(calls, [], "化ける名前で os.open まで届いた")
        self.assertEqual(os.listdir(self.root), [])

    def test_an_alias_spelling_cannot_take_a_second_reservation(self):
        """まだ無い data を書いている間に、別綴り data. で書き込みを開けないこと。"""
        from paramiko import SFTP_FAILURE, SFTPHandle
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        # 1 本目の解決が済み、まだ os.open していない状態を予約で作る
        self.assertTrue(self.writers.reserve(self.handler._get_real_path("/data")))
        for alias in ("/data.", "/data ", "/data::$DATA"):
            with self.subTest(alias=alias):
                rc = self.handler.open(alias, flags, None)
                self.assertNotIsInstance(rc, SFTPHandle)
                self.assertEqual(rc, SFTP_FAILURE)
        self.assertEqual(os.listdir(self.root), [])

    def test_mkdir_remove_rename_refuse_unsafe_names(self):
        from paramiko import SFTP_FAILURE
        with open(os.path.join(self.root, "safe.txt"), "wb") as f:
            f.write(b"x")
        self.assertEqual(self.handler.mkdir("/CON", None), SFTP_FAILURE)
        self.assertEqual(self.handler.mkdir("/dir.", None), SFTP_FAILURE)
        self.assertEqual(self.handler.rename("/safe.txt", "/NUL"), SFTP_FAILURE)
        self.assertEqual(self.handler.rename("/safe.txt", "/safe.txt:ads"), SFTP_FAILURE)
        self.assertEqual(self.handler.remove("/safe.txt."), SFTP_FAILURE)
        self.assertEqual(sorted(os.listdir(self.root)), ["safe.txt"])
        with open(os.path.join(self.root, "safe.txt"), "rb") as f:
            self.assertEqual(f.read(), b"x")


if __name__ == "__main__":
    unittest.main()
