# -*- coding: utf-8 -*-
"""もう閉じた受信ソケットへの abort が、害の無い失敗をログへ出さないことを検証する。

何が起きていたか。_close_raw_socket は、ソケットのオブジェクトがもう閉じて
いる回（受信が終わった直後の abort や、2 回目の abort）でも detach() の
-1 を socket.close() へ渡し、WinError 10038 を「受信ソケットを閉じられません
でした」として記録していた。
"""
import contextlib
import io
import socket
import sys
import types
import unittest

sys.path.insert(0, "src")

from core.version_manager import VersionManager  # noqa: E402


class AbortClosedSocketTest(unittest.TestCase):
    def test_already_closed_socket_is_skipped_quietly(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.close()
        response = types.SimpleNamespace(
            raw=types.SimpleNamespace(
                _connection=types.SimpleNamespace(sock=sock)))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            VersionManager._close_raw_socket(response)
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
