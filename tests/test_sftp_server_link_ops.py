"""SFTP サーバの remove / rename / rmdir が、リンクの先ではなくリンク自体に作用することを検証する。

_get_real_path は最終要素まで realpath で解決していた。ルート内に
alias → target のリンクがあるとき、alias を消す要求は target 自体の削除に、
alias の改名は target の改名になる（実測: ジャンクションで再現）。
利用者は「エイリアスを消した」つもりで本体を失う。

閉じ込めの判定は親ディレクトリを解決して行い、最終要素は解決しないまま
その名前に対して操作する。リンク経由でルートの外へ出る経路
（alias/inside.txt のような「リンクを通る」パス）は、親が外側に解決される
のでこれまでどおり拒否される。
"""
import os
import subprocess
import sys
import tempfile
import unittest

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


class SftpServerLinkOpsTest(unittest.TestCase):
    def setUp(self):
        from core.sftp_server import SFTPServerHandler
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-link-")
        self.target = os.path.join(self.root, "target")
        os.makedirs(self.target)
        self.keep = os.path.join(self.target, "keep.cfg")
        with open(self.keep, "w", encoding="utf-8") as f:
            f.write("hostname R1")
        self.alias = os.path.join(self.root, "alias")
        if make_link(self.alias, self.target) is None:
            self.skipTest("この環境ではディレクトリリンクを作成できない")
        self.handler = SFTPServerHandler(None, self.root)

    def test_rmdir_on_a_link_removes_the_link_and_keeps_the_target(self):
        from paramiko import SFTP_OK
        rc = self.handler.rmdir("/alias")

        self.assertEqual(rc, SFTP_OK)
        self.assertFalse(os.path.lexists(self.alias), "リンクが残っている")
        self.assertTrue(os.path.isdir(self.target), "リンク先のディレクトリが消えた")
        self.assertTrue(os.path.exists(self.keep), "リンク先の中身が消えた")

    def test_rename_of_a_link_renames_the_link_not_the_target(self):
        from paramiko import SFTP_OK
        rc = self.handler.rename("/alias", "/alias2")

        self.assertEqual(rc, SFTP_OK)
        self.assertFalse(os.path.lexists(self.alias))
        self.assertTrue(os.path.lexists(os.path.join(self.root, "alias2")),
                        "改名後のリンクが無い")
        self.assertTrue(os.path.isdir(self.target), "リンク先が改名されている")
        self.assertTrue(os.path.exists(self.keep))

    def test_a_path_through_the_link_still_resolves_inside_the_root(self):
        """対照: リンクを通るパスは、これまでどおり実体に届くこと（ルート内なので許可）。"""
        from paramiko import SFTP_OK
        # alias/keep.cfg は target/keep.cfg。ルート内なので操作できる
        rc = self.handler.remove("/alias/keep.cfg")
        self.assertEqual(rc, SFTP_OK)
        self.assertFalse(os.path.exists(self.keep))

    def test_a_link_pointing_outside_the_root_is_still_confined(self):
        """対照: 外へ出るリンクを通るパスは拒否のまま。"""
        from paramiko import SFTP_FAILURE
        outside = tempfile.mkdtemp(prefix="netbelt-outside-")
        secret = os.path.join(outside, "secret.txt")
        with open(secret, "w", encoding="utf-8") as f:
            f.write("CLASSIFIED")
        escape = os.path.join(self.root, "escape")
        if make_link(escape, outside) is None:
            self.skipTest("この環境ではディレクトリリンクを作成できない")

        rc = self.handler.remove("/escape/secret.txt")

        self.assertEqual(rc, SFTP_FAILURE)
        self.assertTrue(os.path.exists(secret), "ルートの外のファイルが消えた")

    def test_a_plain_directory_is_still_removed(self):
        from paramiko import SFTP_OK
        plain = os.path.join(self.root, "plain")
        os.makedirs(plain)
        self.assertEqual(self.handler.rmdir("/plain"), SFTP_OK)
        self.assertFalse(os.path.exists(plain))


if __name__ == "__main__":
    unittest.main()
