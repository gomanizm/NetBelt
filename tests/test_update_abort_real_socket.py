# -*- coding: utf-8 -*-
"""中止（abort）が、実ソケットで止まった受信をすぐに打ち切ることを検証する。

何が起きていたか。VersionManager.abort() は requests の Response.close() を
呼ぶだけだった。close() は makefile の参照を減らすだけで OS のソケットを
閉じないので、相手が黙り込んだ受信は読み取りのタイムアウト（60 秒）か相手の
切断まで戻らず、その間ダウンロードのスレッドと .part を掴み続けていた。

既存のテストは偽の応答を使うため、この取り違えを検出できない。ここでは
127.0.0.1 に、ヘッダと本文の一部だけ送って黙るサーバーを立てる。
"""
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, "src")

# 中止してから受信が終わるまでの許容（相手を黙らせておく時間より十分短い）
PROMPT = 5.0
HOLD_SECONDS = 60


class _StalledServer:
    """ヘッダと本文の先頭 10 バイトだけ送り、あとは黙るサーバー。"""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.release = threading.Event()
        self.conns = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        self.conns.append(conn)
        try:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\n"
                         b"Content-Length: 1000\r\n"
                         b"Content-Type: application/octet-stream\r\n"
                         b"\r\n" + b"x" * 10)
        except OSError:
            return
        self.release.wait(HOLD_SECONDS)

    def close(self):
        self.release.set()
        for conn in self.conns:
            try:
                conn.close()
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass


class AbortRealSocketTest(unittest.TestCase):

    def test_abort_ends_a_stalled_download_promptly(self):
        """中止後すぐに受信が終わり、.part が残らないこと。"""
        from core.version_manager import VersionManager

        server = _StalledServer()
        self.addCleanup(server.close)
        update_dir = tempfile.mkdtemp(prefix="nb-abort-")

        mgr = VersionManager()
        mgr.UPDATE_DIR = update_dir
        result = {}

        def run():
            base = "http://127.0.0.1:%d/" % server.port
            result["path"] = mgr.download_update(
                base + "a.zip", sha256_url=base + "a.zip.sha256")

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        deadline = time.monotonic() + 10
        while mgr._response is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(mgr._response, "受信が始まらない")
        # 受信ループが recv で止まるまで待つ
        time.sleep(0.5)

        mgr.abort()
        worker.join(PROMPT)

        self.assertFalse(worker.is_alive(),
                         "中止から %.0f 秒たっても受信が終わらない" % PROMPT)
        self.assertIsNone(result.get("path"))
        self.assertEqual(
            [n for n in os.listdir(update_dir) if n.endswith(".part")], [])


if __name__ == "__main__":
    unittest.main()
