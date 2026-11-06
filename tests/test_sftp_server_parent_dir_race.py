# -*- coding: utf-8 -*-
"""内蔵 SFTP サーバーで、まだ無いフォルダへ同時に書いても片方が偽の失敗にならないことを検証する。

何が起きていたか: _open は書き込みのとき親フォルダを
`if not os.path.exists(dir_path): os.makedirs(dir_path)` で作っていた。2 本が
newdir/a.txt と newdir/b.txt を同時に開くと、両方が「無い」と見て makedirs を
呼び、後の方が FileExistsError（WinError 183）で SFTP_FAILURE になった。

どう直したか: FTP・TFTP と同じく os.makedirs(..., exist_ok=True) にする。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SftpServerParentDirRaceTest(unittest.TestCase):
    def test_open_succeeds_when_the_parent_appears_between_check_and_create(self):
        from core.sftp_server import SFTPServerHandler, _OpenWriters
        from paramiko import SFTPHandle
        root = tempfile.mkdtemp(prefix="netbelt-sftp-mkdirs-")
        handler = SFTPServerHandler(None, root, open_writers=_OpenWriters())
        newdir = os.path.join(os.path.realpath(root), "newdir")
        # 先の 1 本が作り終えた状態。こちらの exists の確認はその前に済んだ形にする
        os.makedirs(newdir)
        real_exists = os.path.exists

        def exists_before_the_other(path):
            if os.path.normcase(os.fspath(path)) == os.path.normcase(newdir):
                return False
            return real_exists(path)

        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        with mock.patch("os.path.exists", exists_before_the_other):
            handle = handler.open("/newdir/b.txt", flags, None)
        self.assertIsInstance(handle, SFTPHandle, "親フォルダの同時作成で open が失敗した")
        handle.close()
        self.assertTrue(real_exists(os.path.join(newdir, "b.txt")))


if __name__ == "__main__":
    unittest.main()
