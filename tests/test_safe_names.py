# -*- coding: utf-8 -*-
"""Windows で別の実体に化ける名前を、各サーバーのルート拘束の前に断る共通判定の検証。

realpath と接頭辞の比較は、NUL のような予約デバイス名、`name:stream` の
代替データストリーム、末尾のドットや空白を「ルート配下のパス」として通す。
その後の open は OS 側でデバイス・ストリーム・末尾を除いた名前に解決するので、
TFTP（認証なし）・FTP（匿名の書き込み）・内蔵 SFTP の拘束が素通りしていた。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from core.safe_names import unsafe_name_reason, check_relative_path  # noqa: E402


class UnsafeNameReasonTest(unittest.TestCase):

    def test_plain_names_are_safe(self):
        for name in ("running-config", "backup.cfg", "c9300.bin", "設定.txt", "a.b.c",
                     ".hidden", "normal name with spaces", "CONFIG", "nullable", "com", "lpt10"):
            with self.subTest(name=name):
                self.assertIsNone(unsafe_name_reason(name))

    def test_dot_and_dotdot_are_left_to_the_caller(self):
        self.assertIsNone(unsafe_name_reason("."))
        self.assertIsNone(unsafe_name_reason(".."))

    def test_reserved_device_names_are_refused_in_any_case(self):
        for name in ("NUL", "nul", "Con", "PRN", "aux", "COM1", "com9", "LPT1", "lpt9",
                     "CONIN$", "conout$"):
            with self.subTest(name=name):
                self.assertIsNotNone(unsafe_name_reason(name))

    def test_reserved_device_names_with_an_extension_are_refused(self):
        # Windows 10 は NUL.txt もデバイスとして解決する（Windows 11 では実ファイル）。
        # どちらで動くかは利用者の PC しだいなので、両方で断る
        for name in ("NUL.txt", "nul.log", "COM1.cfg", "lpt1.bin"):
            with self.subTest(name=name):
                self.assertIsNotNone(unsafe_name_reason(name))

    def test_trailing_dots_and_spaces_are_refused(self):
        for name in ("config.", "config ", "config. ", "config..", "a.txt "):
            with self.subTest(name=name):
                self.assertIsNotNone(unsafe_name_reason(name))

    def test_alternate_data_streams_and_drive_letters_are_refused(self):
        for name in ("backup.cfg:evil", "a:b", "data::$DATA", "C:"):
            with self.subTest(name=name):
                self.assertIsNotNone(unsafe_name_reason(name))

    def test_nul_character_is_refused(self):
        self.assertIsNotNone(unsafe_name_reason("a\x00b"))

    def test_the_reason_names_what_was_wrong(self):
        self.assertIn("予約", unsafe_name_reason("NUL"))
        self.assertIn("末尾", unsafe_name_reason("x."))
        self.assertIn("コロン", unsafe_name_reason("a:b"))


class CheckRelativePathTest(unittest.TestCase):

    def test_every_element_is_checked_with_either_separator(self):
        with self.assertRaises(ValueError):
            check_relative_path("sub/NUL")
        with self.assertRaises(ValueError):
            check_relative_path("sub\\backup.cfg:evil")
        with self.assertRaises(ValueError):
            check_relative_path("bad. /ok.txt")

    def test_safe_paths_pass_and_empty_elements_are_ignored(self):
        for path in ("running-config", "sub/dir/file.bin", "/leading/slash", "trailing/slash/",
                     "double//slash", "", "./relative", "../parent"):
            with self.subTest(path=path):
                check_relative_path(path)

    def test_the_error_carries_the_offending_element(self):
        with self.assertRaises(ValueError) as cm:
            check_relative_path("sub/nul.txt")
        self.assertIn("nul.txt", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
