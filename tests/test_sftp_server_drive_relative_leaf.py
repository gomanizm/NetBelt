"""SFTP サーバの削除・改名・ディレクトリ削除が、ドライブ相対の名前で公開ルートを出ないことを検証する。

_get_link_path は最終要素（leaf）をリンク解決せずに扱うため、
os.path.join(親の実パス, leaf) でパスを組み立てている。Windows の
ntpath.join は「公開ルートと別のドライブ文字を持つ相対パス」を渡されると
左側を丸ごと捨てる。実測（公開ルート D:、標的 C:）:

    _get_link_path('C:victim.txt') -> 'C:victim.txt'
    remove rc = SFTP_OK / victim exists after = False

到達先はそのドライブのカレントディレクトリ直下だが、GUI アプリのカレントは
起動ディレクトリ（config.json やホスト鍵の置き場）である。
paramiko はクライアントが送った文字列を加工せず remove/rename/rmdir へ渡すので、
認証済みの相手がこの名前を自由に指定できる。

leaf にドライブ指定が付いた要求は、公開ルートと同じドライブでも拒否する。
Windows ではコロンを含むファイル名は作成できないため、正規の要求では起こらない。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")


def other_drive_letter(path):
    """path が置かれているドライブとは別のドライブ文字を返す。"""
    own = os.path.splitdrive(os.path.abspath(path))[0].upper()
    for letter in "ZYXWV":
        if letter + ":" != own:
            return letter + ":"
    raise AssertionError("別のドライブ文字が見つからない")


class SftpServerDriveRelativeLeafTest(unittest.TestCase):
    def setUp(self):
        if sys.platform != "win32":
            self.skipTest("ドライブ相対パスは Windows だけの表記")
        from core.sftp_server import SFTPServerHandler
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-drive-")
        self.root_real = os.path.realpath(self.root)
        self.handler = SFTPServerHandler(None, self.root)
        self.other = other_drive_letter(self.root)
        self.own = os.path.splitdrive(self.root_real)[0]

    def _names(self):
        return ["/%svictim.txt" % self.other, "/%svictim.txt" % self.own]

    def test_a_drive_relative_leaf_is_refused(self):
        for name in self._names():
            with self.subTest(name=name):
                with self.assertRaises(IOError):
                    self.handler._get_link_path(name)

    def test_remove_rename_rmdir_touch_nothing_for_a_drive_relative_leaf(self):
        """OS 呼び出しへ到達しないことまで見る。

        脱出先が実在しない環境では os 側の例外で SFTP_FAILURE が返るため、
        戻り値だけでは「拒否した」と「消そうとして失敗した」を区別できない。
        os.remove / os.rename / os.rmdir を記録役に差し替えて、そもそも
        呼ばれないことを確かめる。
        """
        from unittest import mock
        from paramiko import SFTP_FAILURE

        for name in self._names():
            with self.subTest(name=name):
                calls = []
                with mock.patch("os.remove", lambda *a: calls.append(("remove",) + a)), \
                     mock.patch("os.rmdir", lambda *a: calls.append(("rmdir",) + a)), \
                     mock.patch("os.rename", lambda *a: calls.append(("rename",) + a)):
                    rcs = [
                        self.handler.remove(name),
                        self.handler.rmdir(name),
                        self.handler.rename(name, "/safe.txt"),
                        self.handler.rename("/safe.txt", name),
                    ]

                self.assertEqual(calls, [], "拒否すべき要求が OS 呼び出しへ届いた")
                self.assertEqual(rcs, [SFTP_FAILURE] * 4)

    def test_a_plain_leaf_still_resolves_under_the_root(self):
        got = self.handler._get_link_path("/keep.cfg")

        self.assertEqual(got, os.path.join(self.root_real, "keep.cfg"))


if __name__ == "__main__":
    unittest.main()
