"""SFTP のルートにドライブ直下（D:\ など）を指定しても配下へ届くことを確認する。

閉じ込め判定は `real_path.startswith(root_real + os.sep)` だった。ルートが
ドライブ直下だと realpath は 'D:\' で終わるため接頭辞が 'D:\\' になり、
直下・配下のあらゆるパスが Access denied になる（一覧だけは '/' の等価
判定で通るので、見えるのに取れない紛らわしい状態）。実測: root='D:\' で
'/file.cfg' -> DENIED、'/sub/x.cfg' -> DENIED、対照の 'C:\Windows' は全て OK。

ファイルは作らず純粋なパス論理だけを検査する（realpath は存在しない
パスも解決する）。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")


class SftpDriveRootTest(unittest.TestCase):
    def setUp(self):
        from core.sftp_server import SFTPServerHandler
        drive = os.path.splitdrive(os.path.realpath(tempfile.gettempdir()))[0]
        # Windows 以外（ドライブ文字が無い環境）では '/' がルート直下に相当する
        self.drive_root = (drive + os.sep) if drive else os.sep
        self.handler = SFTPServerHandler(None, self.drive_root)

    def test_root_itself_is_reachable(self):
        self.assertEqual(self.handler._get_real_path("/"),
                         os.path.realpath(self.drive_root))

    def test_files_directly_under_a_drive_root_are_reachable(self):
        for p in ("/file.cfg", "file.cfg", "/sub/x.cfg"):
            with self.subTest(path=p):
                try:
                    real = self.handler._get_real_path(p)
                except IOError as e:
                    self.fail("ドライブ直下ルートで %r が拒否された: %s" % (p, e))
                expected = os.path.realpath(
                    os.path.join(self.drive_root, p.lstrip("/").replace("/", os.sep)))
                self.assertEqual(real, expected)

    def test_traversal_out_of_a_normal_root_is_still_denied(self):
        """接頭辞の直し方で、通常ルートの閉じ込めが緩まないこと。"""
        from core.sftp_server import SFTPServerHandler
        root = tempfile.mkdtemp(prefix="netbelt-sftp-root-")
        h = SFTPServerHandler(None, root)
        # 'root' と 'root2' のように前方一致するだけの隣のディレクトリは外
        sibling = os.path.basename(root) + "2"
        with self.assertRaises(IOError):
            h._get_real_path("/../" + sibling + "/x.cfg")
        with self.assertRaises(IOError):
            h._get_real_path("/../../x.cfg")
        self.assertEqual(h._get_real_path("/a/b.cfg"),
                         os.path.realpath(os.path.join(root, "a", "b.cfg")))


if __name__ == "__main__":
    unittest.main()
