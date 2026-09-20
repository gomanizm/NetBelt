"""一覧の直後に mibs/ のファイルが消えても、MIB 読み込みが続くことを検証する。

_mib_file_stamp は os.stat(path) を try で囲っていなかった。os.listdir →
os.path.isfile を通った直後にそのファイルが消える（ウイルス対策の隔離、
起動中の入れ替え、同期クライアント）と、FileNotFoundError が
MIBResolver.__init__ まで素通りする。

実測（B.my が os.path.isfile の直後に消える細工）:
- MIBResolver() が FileNotFoundError を投げ、生き残っている A.my の定義まで
  1 件も読めない。
- 画面には何も出ない。標準出力に「バックグラウンドMIB読み込みエラー」が
  1 行出るだけで、mib_loaded は True になる。

直し方: 印を取れなかったファイルは、読めない MIB と同じように理由を 1 行
残して読み込みから外し、残りのファイルで続ける。
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

A_MIB = ("A-MIB DEFINITIONS ::= BEGIN\r\n"
         "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }\r\n"
         "aTrap OBJECT IDENTIFIER ::= { aRoot 1 }\r\n"
         "END\r\n")
B_MIB = ("B-MIB DEFINITIONS ::= BEGIN\r\n"
         "bRoot OBJECT IDENTIFIER ::= { enterprises 2222 }\r\n"
         "END\r\n")


class MibFileVanishesAfterListingTest(unittest.TestCase):
    def setUp(self):
        # exe の隣に mibs/ を置く凍結ビルドとして動かす
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-vanish-")
        self.addCleanup(shutil.rmtree, self.exe_dir, True)
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)
        mibs = os.path.join(self.exe_dir, "mibs")
        os.makedirs(mibs)
        for name, text in (("A.my", A_MIB), ("B.my", B_MIB)):
            with io.open(os.path.join(mibs, name), "w",
                         encoding="utf-8", newline="") as f:
                f.write(text)
        self.victim = os.path.join(mibs, "B.my")

    def _load_with_vanishing_file(self):
        """B.my が os.path.isfile の直後に消える状況で読み込む

        Returns:
            (MIBResolver, 標準出力)
        """
        real_isfile = os.path.isfile
        victim = self.victim

        def isfile_then_vanish(path):
            ok = real_isfile(path)
            if ok and os.path.basename(path) == "B.my" and real_isfile(victim):
                os.remove(victim)
            return ok

        from core.mib_resolver import MIBResolver
        out = io.StringIO()
        with mock.patch("os.path.isfile", side_effect=isfile_then_vanish):
            with contextlib.redirect_stdout(out):
                resolver = MIBResolver()
        return resolver, out.getvalue()

    def test_the_remaining_mibs_are_still_loaded(self):
        """消えた 1 件のために、残りの MIB まで落とさないこと。"""
        resolver, _ = self._load_with_vanishing_file()
        self.assertEqual(resolver.resolve_name("aTrap"),
                         "1.3.6.1.4.1.1111.1",
                         "生き残ったファイルの定義まで読めていない")

    def test_the_vanished_file_is_named_once(self):
        """外したファイルの名前と理由を、1 行残すこと。"""
        _, printed = self._load_with_vanishing_file()
        lines = [line for line in printed.splitlines() if "B.my" in line]
        self.assertEqual(len(lines), 1,
                         "何が外れたか分からない: %r" % printed)

    def test_the_vanished_file_is_not_recorded_in_the_cache(self):
        """消えたファイルを、解析済みとしてキャッシュに残さないこと。"""
        import json
        self._load_with_vanishing_file()
        cache = os.path.join(self.exe_dir, "mib_cache.json")
        with io.open(cache, encoding="utf-8") as f:
            data = json.load(f)
        self.assertNotIn("B.my", data.get("files", {}))
        self.assertIn("A.my", data.get("files", {}))


if __name__ == "__main__":
    unittest.main()
