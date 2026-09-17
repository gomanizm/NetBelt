"""READDIR がリンク先ではなくリンク自身の属性を返すことを検証する。

LSTAT は直ったが、隣の READDIR（list_folder）は os.stat のままリンクを
解決していたため、同じ名前に対して一覧と LSTAT が別の属性を返していた。
SFTP の READDIR は lstat 相当を返すのが慣例で、クライアント側
（core/sftp_manager.py）もリンクを見つけたら自分で引き直す作りになっている。

実測（Windows 11 / この venv の Python 3.12、mklink /J のジャンクション。
リンク先 target の mtime だけ 2000-01-01 に設定）:

    alias mtime in READDIR = Sat Jan  1 09:00:00 2000  -> リンク先
    lstat('/alias') mtime  = Tue Sep 15 00:32:24 2026  -> リンク自身

残る制限: Windows のジャンクションは os.lstat でも S_IFLNK が立たない
（このマシンの実測で st_mode = 0o40777 = ディレクトリ扱い）。そのため
ジャンクションだけは、一覧の側からリンクだと見分けることはできない。
開発者モード等で作れる本来のシンボリックリンクでは S_IFLNK が立つ。
"""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, "src")

LINK_TARGET_MTIME = 946684800  # 2000-01-01T00:00:00Z


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


class SftpServerListingLinkAttrsTest(unittest.TestCase):
    def setUp(self):
        from core.sftp_server import SFTPServerHandler
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-listattr-")
        self.target = os.path.join(self.root, "target")
        os.makedirs(self.target)
        self.alias = os.path.join(self.root, "alias")
        if make_link(self.alias, self.target) is None:
            self.skipTest("この環境ではディレクトリリンクを作成できない")
        # リンク先だけ過去の時刻にして、どちらを見ているか区別できるようにする
        os.utime(self.target, (LINK_TARGET_MTIME, LINK_TARGET_MTIME))
        self.handler = SFTPServerHandler(None, self.root)

    def _entry(self, name, path="/"):
        from paramiko import SFTP_FAILURE
        items = self.handler.list_folder(path)
        self.assertNotEqual(items, SFTP_FAILURE, "list_folder が失敗した")
        for attr in items:
            if attr.filename == name:
                return attr
        self.fail(f"{name} が一覧に無い")

    def test_readdir_reports_the_link_itself_not_its_target(self):
        entry = self._entry("alias")

        self.assertNotEqual(int(entry.st_mtime), LINK_TARGET_MTIME,
                            "一覧がリンク先の属性を返している")
        self.assertEqual(int(entry.st_mtime), int(os.lstat(self.alias).st_mtime))

    def test_readdir_and_lstat_agree_on_the_same_name(self):
        from paramiko import SFTP_FAILURE
        entry = self._entry("alias")
        attr = self.handler.lstat("/alias")

        self.assertNotEqual(attr, SFTP_FAILURE)
        self.assertEqual(int(entry.st_mtime), int(attr.st_mtime),
                         "一覧と LSTAT が同じ名前に別の答えを返している")

    def test_the_link_target_itself_is_still_reported_as_is(self):
        """対照: リンク先そのものは、これまでどおり自分の属性で出る。"""
        entry = self._entry("target")

        self.assertEqual(int(entry.st_mtime), LINK_TARGET_MTIME)

    def test_a_plain_file_still_reports_its_own_size(self):
        """対照: ふつうのファイルの属性は変わらない。"""
        plain = os.path.join(self.root, "plain.cfg")
        with open(plain, "w", encoding="utf-8") as f:
            f.write("hostname R1")

        self.assertEqual(self._entry("plain.cfg").st_size,
                         os.path.getsize(plain))


if __name__ == "__main__":
    unittest.main()
