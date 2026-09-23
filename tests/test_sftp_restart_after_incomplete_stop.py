"""srv-01 の積み残し: 停止しきれていないのに SFTP を再起動でき、中身が混ざる件。

何が起きていたか（実測、基準 097550c。127.0.0.1 のみ）: TFTP では
「停止の期限を過ぎても転送が生き残っている間は再起動を断る」ように直したが、
SFTP には同じ防御が無かった。SFTPServerManager.stop() は接続を閉じて
待受スレッドと接続ごとのハンドラを待つだけで、実際にファイルを書いている
paramiko の SFTP スレッド（SFTPServer）は見ていない。

  1) 旧サーバでアップロードが 1 件走り、サーバ側の write() の中で止まる
     （共有フォルダの遅延・切断を模して、write() を合図まで止めた）
  2) stop() は 0.5 秒で戻り、is_running=False。書き込み中のスレッド
     'Thread-8 (_run)' は保存先を開いたまま生き残る
  3) 同じ root/port で start() が True を返し、同名への新しいアップロード
     b'NEW-CONTENT-NEW-CONTENT' がそのまま通る
  4) 旧 write() が戻ると、握っていたファイルの先頭へ書き込み、
     ファイルは b'OLD-CONTENT-NEW-CONTENT' に混ざる

機器の config を受け取る道具なので、黙って壊れた内容が残るのは許容できない。

利用者の決定（2026-09-23、TFTP の srv-01 と同じ扱い）: 再起動を断る。
停止のあとも生き残っている書き込みがある間は、次の start() を
「前回の停止が完了していません」と断る。待たないので画面は固まらない。
生き残りが消えたら、これまでどおり起動できる。

どう直したか: SFTPServerHandler.open() が書き込み用に開いたハンドルを、
扱っているスレッド・実パスと一緒にマネージャの一覧へ登録し、ハンドルを
閉じ終えたら外す（close() の中で止まっている間は残る）。stop() の最後に、
一覧に残っていて生きているスレッドを覚え、終わりきれたかを返す。start() は
その生き残りがいる間、TFTP と同じ文言（sftp_server.
PREVIOUS_STOP_INCOMPLETE_MESSAGE）を error_occurred で伝えて False を返す。
"""
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

USER = "tester"
PASSWORD = "example-pass"
OLD = b"OLD-CONTENT"
NEW = b"NEW-CONTENT-NEW-CONTENT"


def free_tcp_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _BlockingFile:
    """write() か close() が合図まで戻らないファイル。

    共有フォルダの遅延・切断で、サーバ側の書き出しが長く掛かる状況を模す。
    """

    def __init__(self, handle, stuck_in, gate, entered):
        self._handle = handle
        self._stuck_in = stuck_in
        self._gate = gate
        self._entered = entered

    def write(self, data):
        if self._stuck_in == "write":
            self._entered.set()
            self._gate.wait(30)
        return self._handle.write(data)

    def close(self):
        if self._stuck_in == "close":
            self._entered.set()
            self._gate.wait(30)
        self._handle.close()

    def __getattr__(self, name):
        return getattr(self._handle, name)


class SftpRestartAfterIncompleteStopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import paramiko
        cls.host_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        import core.sftp_server as sftp_server
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        home = mock.patch("core.config_manager.app_data_dir",
                          return_value=data_dir)
        home.start()
        self.addCleanup(home.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-restart-")
        self.port = free_tcp_port()
        self.gate = threading.Event()       # 止めた書き出しを通す合図
        self.entered = threading.Event()    # 書き出しで止まった合図
        self.stuck_in = {"at": None}        # "write" / "close" / None
        real_open = sftp_server.SFTPServerHandler.open
        gate, entered, stuck_in = self.gate, self.entered, self.stuck_in

        def patched_open(handler, path, flags, attr):
            fobj = real_open(handler, path, flags, attr)
            writefile = getattr(fobj, "writefile", None)
            if writefile is not None and stuck_in["at"]:
                fobj.writefile = _BlockingFile(writefile, stuck_in["at"],
                                               gate, entered)
            return fobj

        patcher = mock.patch.object(sftp_server.SFTPServerHandler, "open",
                                    patched_open)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.gate.set()   # 生き残りを解放してから後片付けへ入る

    def _manager(self):
        from core.sftp_server import SFTPServerManager
        manager = SFTPServerManager()
        manager.host_key = self.host_key
        self.addCleanup(manager.stop)
        return manager

    def _start(self, manager, port=None, root=None):
        return manager.start(port=port or self.port, root_dir=root or self.root,
                             username=USER, password=PASSWORD)

    def _connect(self, port=None):
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect("127.0.0.1", port=port or self.port, username=USER,
                       password=PASSWORD, look_for_keys=False,
                       allow_agent=False, timeout=10)
        return client

    def _upload(self, payload):
        client = self._connect()
        try:
            sftp = client.open_sftp()
            with sftp.open("config.cfg", "wb") as remote:
                remote.write(payload)
        finally:
            client.close()

    def _leave_an_upload_stuck(self, manager, stuck_in):
        """アップロードを 1 件走らせ、サーバ側の write()/close() で止める。"""
        self.stuck_in["at"] = stuck_in
        self.assertTrue(self._start(manager))

        def send():
            try:
                self._upload(OLD)
            except Exception:
                pass   # 停止で切られるのは想定どおり

        threading.Thread(target=send, daemon=True).start()
        self.assertTrue(self.entered.wait(20),
                        "アップロードが %s() まで進んでいない（前提が崩れている）"
                        % stuck_in)
        self.stuck_in["at"] = None

    def _read_target(self):
        with open(os.path.join(self.root, "config.cfg"), "rb") as handle:
            return handle.read()

    def test_start_is_refused_while_a_stuck_write_survives(self):
        """write() で止まった書き込みが残る間の起動を断り、理由を伝えること。"""
        manager = self._manager()
        self._leave_an_upload_stuck(manager, "write")

        finished = manager.stop()
        self.assertFalse(manager.is_running)

        errors = []
        manager.error_occurred.connect(errors.append)
        began = time.monotonic()
        started = self._start(manager)
        elapsed = time.monotonic() - began

        self.assertFalse(started, "生き残りがいるのに起動を受け付けている")
        from core.sftp_server import PREVIOUS_STOP_INCOMPLETE_MESSAGE
        self.assertIn(PREVIOUS_STOP_INCOMPLETE_MESSAGE, errors,
                      "断った理由を伝えていない: %r" % (errors,))
        self.assertLess(elapsed, 3.0,
                        "断るのに待っている（画面が固まる）: %.1f秒" % elapsed)
        self.assertFalse(manager.is_running)
        self.assertIs(finished, False,
                      "stop() が生き残りを知らせていない: %r" % (finished,))
        # 別のポート・別のルートでも、生き残りがいる間は断る
        other_root = tempfile.mkdtemp(prefix="netbelt-sftp-other-")
        self.assertFalse(self._start(manager, port=free_tcp_port(),
                                     root=other_root),
                         "別のポート・ルートなら生き残りがいても起動している")

    def test_start_is_refused_while_a_stuck_close_survives(self):
        """close() で止まった書き込みが残る間も起動を断ること。"""
        manager = self._manager()
        self._leave_an_upload_stuck(manager, "close")

        manager.stop()

        self.assertFalse(self._start(manager),
                         "生き残りがいるのに起動を受け付けている")
        self.assertFalse(manager.is_running)

    def test_start_works_again_once_the_survivor_is_gone(self):
        """生き残りが消えたら起動でき、新しいアップロードが混ざらないこと。"""
        manager = self._manager()
        self._leave_an_upload_stuck(manager, "write")
        manager.stop()
        self.assertFalse(self._start(manager),
                         "生き残りがいるのに起動を受け付けている")

        self.gate.set()                 # 旧アップロードの write() を通す
        started = False
        deadline = time.time() + 15
        while not started and time.time() < deadline:
            started = self._start(manager)
            if not started:
                time.sleep(0.1)
        self.assertTrue(started,
                        "生き残りが消えても起動できないままになっている")

        self._upload(NEW)
        time.sleep(0.5)   # 旧スレッドの書き戻しがあれば、ここまでに起きる
        self.assertEqual(self._read_target(), NEW,
                         "新しいアップロードの中身が旧転送と混ざっている")

    def test_a_clean_stop_still_allows_a_restart(self):
        """書き込みの残らない普通の停止は、これまでどおりすぐ起動できること。"""
        manager = self._manager()
        self.assertTrue(self._start(manager))
        self._upload(NEW)

        finished = manager.stop()
        self.assertTrue(self._start(manager), "普通に停止した後に起動できない")
        self.assertIs(finished, True,
                      "普通の停止なのに生き残りがいると返している")

    def test_an_upload_left_open_does_not_block_the_restart(self):
        """開いたまま止まっていない書き込みは、停止で閉じて起動を妨げないこと。"""
        manager = self._manager()
        self.assertTrue(self._start(manager))
        client = self._connect()
        self.addCleanup(client.close)
        remote = client.open_sftp().open("config.cfg", "wb")
        remote.write(OLD)
        remote.flush()

        finished = manager.stop()
        self.assertTrue(self._start(manager),
                        "止まっていない書き込みのせいで起動できない")
        self.assertIs(finished, True,
                      "止まっていない書き込みを生き残りとして数えている")


if __name__ == "__main__":
    unittest.main()
