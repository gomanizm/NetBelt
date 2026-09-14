"""SFTP サーバの LSTAT が、リンク先ではなくリンク自身の情報を返すことを検証する。

lstat は _get_real_path（最終要素まで realpath で解決）を使っていたため、
LSTAT が STAT と同じ意味になっていた。os.lstat 自体はリンクを解決しないので、
渡す前に解決してしまうとリンク自身の属性は二度と取れない。

実測（Windows 11 / この venv の Python、mklink /J のジャンクション）:

    os.lstat(alias).st_ino  = 281474977910536   （リンク自身）
    os.lstat(target).st_ino = 844424931331826   （リンク先）
    os.lstat(alias).st_file_attributes = 1040   （FILE_ATTRIBUTE_REPARSE_POINT 付き）

閉じ込めの判定は親ディレクトリを解決して行い、最終要素はその名前のまま
os.lstat へ渡す。リンクを通ってルートの外へ出るパスは、親が外側に解決される
ので従来どおり拒否される。
"""
import os
import stat as stat_module
import subprocess
import sys
import tempfile
import unittest
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


class SftpServerLstatLinkTest(unittest.TestCase):
    def setUp(self):
        from core.sftp_server import SFTPServerHandler
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-lstat-")
        self.target = os.path.join(self.root, "target")
        os.makedirs(self.target)
        self.alias = os.path.join(self.root, "alias")
        if make_link(self.alias, self.target) is None:
            self.skipTest("この環境ではディレクトリリンクを作成できない")
        self.handler = SFTPServerHandler(None, self.root)

    def _make_outside_link(self):
        outside = tempfile.mkdtemp(prefix="netbelt-outside-")
        with open(os.path.join(outside, "secret.txt"), "w", encoding="utf-8") as f:
            f.write("CLASSIFIED")
        escape = os.path.join(self.root, "escape")
        if make_link(escape, outside) is None:
            self.skipTest("この環境ではディレクトリリンクを作成できない")
        return outside, escape

    def test_lstat_passes_the_unresolved_leaf_to_os_lstat(self):
        from paramiko import SFTP_FAILURE
        seen = []
        real_lstat = os.lstat

        def spy(path, *args, **kwargs):
            seen.append(os.path.normcase(str(path)))
            return real_lstat(path, *args, **kwargs)

        with mock.patch("os.lstat", spy):
            attr = self.handler.lstat("/alias")

        self.assertNotEqual(attr, SFTP_FAILURE)
        self.assertIn(os.path.normcase(self.alias), seen,
                      "リンク自身のパスで os.lstat が呼ばれていない")
        self.assertNotIn(os.path.normcase(self.target), seen,
                         "リンク先まで解決してから os.lstat を呼んでいる")

    def test_lstat_of_a_link_pointing_outside_the_root_describes_the_link(self):
        """外を指すリンクでも、リンク自身の属性は返せる（リンク先はたどらない）。"""
        from paramiko import SFTP_FAILURE
        self._make_outside_link()

        attr = self.handler.lstat("/escape")

        self.assertNotEqual(attr, SFTP_FAILURE)
        self.assertTrue(stat_module.S_ISDIR(attr.st_mode))

    def test_a_path_through_a_link_to_the_outside_is_still_denied(self):
        """対照: リンクを通ってルートの外へ出るパスは拒否のまま。"""
        from paramiko import SFTP_FAILURE
        self._make_outside_link()

        self.assertEqual(self.handler.lstat("/escape/secret.txt"), SFTP_FAILURE)

    def test_lstat_of_the_root_itself_still_works(self):
        from paramiko import SFTP_FAILURE
        attr = self.handler.lstat("/")

        self.assertNotEqual(attr, SFTP_FAILURE)
        self.assertTrue(stat_module.S_ISDIR(attr.st_mode))

    def test_lstat_of_a_plain_file_reports_its_own_size(self):
        from paramiko import SFTP_FAILURE
        plain = os.path.join(self.root, "plain.cfg")
        with open(plain, "w", encoding="utf-8") as f:
            f.write("hostname R1")

        attr = self.handler.lstat("/plain.cfg")

        self.assertNotEqual(attr, SFTP_FAILURE)
        self.assertEqual(attr.st_size, os.path.getsize(plain))


if __name__ == "__main__":
    unittest.main()
