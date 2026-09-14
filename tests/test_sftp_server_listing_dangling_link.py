"""READDIR がリンク先を失ったリンクを取りこぼさないことを検証する。

list_folder は各項目を os.stat で引いていたため、リンク先が消えた
ジャンクションのように「実体はあるが追跡できない」項目が
`except Exception: continue` で黙って捨てられていた。ディスク上には存在し、
名前を指定すれば RMDIR で消せるのに、一覧には一切現れない。LSTAT が
リンク自身を記述できるようになった結果、一覧と LSTAT の答えが食い違う。

実測（Windows 11 / この venv の Python 3.12、mklink /J のジャンクションの
リンク先を rmdir で消したもの）:

    entries on disk           : ['alias', 'dangle', 'target']
    entries in READDIR reply  : ['alias', 'target']
    lstat('/dangle')          : attributes returned
    stat ('/dangle')          : SFTP_FAILURE

さらに、どうしても描写できない項目を落とすときは黙って捨てず、理由を
ログへ出すこと（原因の分からない「一覧に出ない」を防ぐため）。
"""
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, "src")


def make_link(link_path, target_dir):
    """ディレクトリへのリンクを作る。作れない環境では None を返す。"""
    if sys.platform == "win32":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link_path, target_dir],
                           capture_output=True)
        return link_path if r.returncode == 0 else None
    try:
        os.symlink(target_dir, link_path)
        return link_path
    except OSError:
        return None


class SftpServerListingDanglingLinkTest(unittest.TestCase):
    def setUp(self):
        from core.sftp_server import SFTPServerHandler
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-dangle-")
        self.target = os.path.join(self.root, "target")
        os.makedirs(self.target)
        self.handler = SFTPServerHandler(None, self.root)

    def _make_dangling_link(self, name="dangle"):
        """リンク先を失ったリンクを root 直下に作る。"""
        gone = tempfile.mkdtemp(prefix="netbelt-gone-")
        link = os.path.join(self.root, name)
        if make_link(link, gone) is None:
            self.skipTest("この環境ではディレクトリリンクを作成できない")
        os.rmdir(gone)
        return link

    def _names(self, path="/"):
        from paramiko import SFTP_FAILURE
        items = self.handler.list_folder(path)
        self.assertNotEqual(items, SFTP_FAILURE, "list_folder が失敗した")
        return sorted(a.filename for a in items)

    def test_a_dangling_link_is_listed(self):
        self._make_dangling_link()

        self.assertIn("dangle", self._names(),
                      "リンク先を失ったリンクが READDIR の応答から消えている")

    def test_the_listing_matches_what_is_on_disk(self):
        self._make_dangling_link()

        self.assertEqual(self._names(), sorted(os.listdir(self.root)))

    def test_a_dangling_link_that_lstat_describes_is_not_dropped(self):
        """対照: LSTAT が答えられる名前は、一覧にも出ていること。"""
        from paramiko import SFTP_FAILURE
        self._make_dangling_link()

        self.assertNotEqual(self.handler.lstat("/dangle"), SFTP_FAILURE)
        self.assertIn("dangle", self._names())

    def test_an_entry_that_cannot_be_described_is_reported(self):
        """それでも引けない項目は落としてよいが、黙って捨てないこと。"""
        broken = os.path.join(self.root, "broken.cfg")
        with open(broken, "w", encoding="utf-8") as f:
            f.write("hostname R1")
        real_lstat = os.lstat

        def flaky(path, *args, **kwargs):
            if os.path.normcase(str(path)) == os.path.normcase(broken):
                raise OSError("Access denied")
            return real_lstat(path, *args, **kwargs)

        buf = io.StringIO()
        with mock.patch("os.lstat", flaky), redirect_stdout(buf):
            names = self._names()

        self.assertNotIn("broken.cfg", names)
        self.assertIn("broken.cfg", buf.getvalue(),
                      "描写できなかった項目が黙って捨てられている")

    def test_plain_entries_are_still_listed(self):
        """対照: ふつうのファイルとディレクトリは従来どおり。"""
        plain = os.path.join(self.root, "plain.cfg")
        with open(plain, "w", encoding="utf-8") as f:
            f.write("hostname R1")

        self.assertEqual(self._names(), ["plain.cfg", "target"])


if __name__ == "__main__":
    unittest.main()
