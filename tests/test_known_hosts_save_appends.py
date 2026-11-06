# -*- coding: utf-8 -*-
"""known_hosts の保存が、利用者の書いた行をそのまま残すこと。

初回接続の鍵を 1 件保存するたびに、_save_known_hosts はファイル全体を
paramiko の HostKeys.save で書き直していた。そのため:

- コメント行（#）と空行が消える
- 4 列目以降の注釈（「honsha-core-01」のようなメモ）が消える
- カンマ区切りの名前の行（192.0.2.2,sw2.example.com ...）が名前ごとに割れる
- @revoked / @cert-authority の行が元の位置から末尾へ移る

OpenSSH と同じく、保存は「今のファイルの末尾に 1 行足す」だけにする。
同じ鍵の行がすでにあれば何も書かない。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

import paramiko                                     # noqa: E402


class KnownHostsSaveAppendsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()
        cls.key_c = paramiko.ECDSAKey.generate()
        cls.key_new = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khappend-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.dir / "known_hosts"

    @staticmethod
    def _line(names, key, annotation=""):
        text = "%s %s %s" % (names, key.get_name(), key.get_base64())
        return text + (" " + annotation if annotation else "")

    def _user_file(self):
        """利用者が手で整えた known_hosts の中身（bytes）"""
        lines = [
            "# main router (replaced 2026-01)",
            self._line("192.0.2.1", self.key_a, "honsha-core-01"),
            "",
            "# spare",
            self._line("192.0.2.2,sw2.example.com", self.key_b, "yobi"),
            "@revoked 192.0.2.3 " + self.key_c.get_name() + " "
            + self.key_c.get_base64(),
            self._line("192.0.2.4", self.key_c),
        ]
        return ("\r\n".join(lines) + "\r\n").encode("utf-8")

    def _save(self, hostname, key):
        from core.ssh_connection import _save_known_hosts
        _save_known_hosts(self.path, (hostname, key))

    def test_save_keeps_existing_bytes_and_appends_one_line(self):
        """保存の後も元の中身が先頭にそのまま残り、新しい行が 1 本だけ足されること。"""
        before = self._user_file()
        self.path.write_bytes(before)

        self._save("198.51.100.9", self.key_new)

        after = self.path.read_bytes()
        self.assertTrue(
            after.startswith(before),
            "元の行（コメント・空行・注釈・@ 印の位置）が保存で変わった:\n%r"
            % after)
        added = after[len(before):].decode("ascii").splitlines()
        self.assertEqual(
            [self._line("198.51.100.9", self.key_new)], added,
            "足された行が今回の 1 件になっていない")

    def test_save_of_a_known_key_writes_nothing(self):
        """同じ名前・同じ鍵の行がすでにあれば、ファイルを変えないこと。"""
        before = self._user_file()
        self.path.write_bytes(before)

        self._save("sw2.example.com", self.key_b)

        self.assertEqual(before, self.path.read_bytes(),
                         "同じ鍵がすでにあるのに書き換えている")

    def test_missing_final_newline_is_added_before_the_new_line(self):
        """末尾に改行の無いファイルでも、最後の行と新しい行がつながらないこと。"""
        last = self._line("192.0.2.1", self.key_a, "no-newline")
        self.path.write_bytes(last.encode("ascii"))

        self._save("198.51.100.9", self.key_new)

        lines = self.path.read_bytes().decode("ascii").splitlines()
        self.assertEqual(
            [last, self._line("198.51.100.9", self.key_new)], lines)

    def test_bom_is_dropped_and_rest_kept(self):
        """先頭の UTF-8 BOM は剥がし、残りの中身はそのまま残すこと。"""
        body = self._user_file()
        self.path.write_bytes(b"\xef\xbb\xbf" + body)

        self._save("198.51.100.9", self.key_new)

        after = self.path.read_bytes()
        self.assertTrue(after.startswith(body),
                        "BOM の後ろの中身が保存で変わった: %r" % after[:80])

    def test_new_file_gets_just_the_new_line(self):
        """ファイルが無ければ、新しい 1 行だけのファイルができること。"""
        self._save("198.51.100.9", self.key_new)

        self.assertEqual(
            [self._line("198.51.100.9", self.key_new)],
            self.path.read_bytes().decode("ascii").splitlines())

    def test_only_identical_key_lines_are_folded(self):
        """畳むのは同じ鍵の行の重複だけで、重なったコメント行・空行は残すこと。"""
        same = self._line("192.0.2.1", self.key_a)
        lines = ["# note", "# note", "", "", same, same]
        self.path.write_bytes(("\r\n".join(lines) + "\r\n").encode("ascii"))

        self._save("198.51.100.9", self.key_new)

        self.assertEqual(
            ["# note", "# note", "", "", same,
             self._line("198.51.100.9", self.key_new)],
            self.path.read_bytes().decode("ascii").splitlines())

    def test_non_utf8_lines_with_different_names_are_not_folded(self):
        """UTF-8 で読めない名前（cp932 の日本語名）の行は、同じ鍵でも名前が違えば両方残すこと。"""
        east = self._line("東", self.key_a).encode("cp932")
        capital = self._line("京", self.key_a).encode("cp932")
        before = east + b"\r\n" + capital + b"\r\n"
        self.path.write_bytes(before)

        self._save("198.51.100.9", self.key_new)

        after = self.path.read_bytes()
        self.assertTrue(
            after.startswith(before),
            "名前の違う cp932 の行が重複として落とされた: %r" % after)


if __name__ == "__main__":
    unittest.main()
