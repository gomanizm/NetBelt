"""書き込み権限を伴わない作成の OPEN（O_CREAT / O_EXCL）も、停止の管理に入れること。

何が起きていたか（実測、c2bb66a。127.0.0.1 のみ）: SFTPServerHandler.open が停止に
見せる（_busy で一覧へ載せ、停止フラグを見る）のは、書き込み権限（WRITE）か
O_TRUNC を伴う OPEN だけだった。ところが paramiko のサーバーは CREATE だけの
OPEN を O_RDONLY|O_CREAT に、クライアントの open(name, 'x') を
O_RDONLY|O_CREAT|O_EXCL にして渡し、os.open はどちらも存在しない名前の
空ファイルを作る。そのため:

  - 停止フラグを立てたハンドラへ、存在しない名前で O_RDONLY|O_CREAT を渡すと、
    断られずに SFTPHandle が返り、空ファイルができた（O_EXCL 付きも同じ。
    対照の O_WRONLY|O_CREAT は 'Refused, the server has been stopped' で断られた）
  - 止まった STAT の後ろに OPEN(CREATE) / OPEN(CREATE|EXCL) を積むと、
    stop() は True、start() も True で、再起動の後に旧スレッドがその OPEN を
    実行して空ファイルを作った
  - 共有フォルダで止まった作成の OPEN は、停止の一覧に載らないので生き残りに
    数えられない

どう直したか: 作りうる OPEN（O_CREAT / O_EXCL を含む）も、パスの解決から開き
終えるまで _busy の中で行う。停止の後に届いたものは断り（SFTP_FAILURE）、
止まっているものは停止が生き残りとして数えて次の起動を断る。書き込みの予約
（同じ保存先を 2 本が書かないための仕組み）は今までどおり書き込みと切り詰めの
OPEN だけに掛ける。作成だけの OPEN は既存のファイルを変えないので、書き込み中
の保存先への作成だけの OPEN も今までどおり通す。
"""
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

USER = "tester"
PASSWORD = "example-pass"
SLOW = "slow.cfg"
NEW = "created.cfg"

# 作成だけの OPEN（書き込み権限なし）。paramiko のサーバーが渡す os のフラグ
CREATE_ONLY = {
    "O_RDONLY|O_CREAT": os.O_RDONLY | os.O_CREAT,
    "O_RDONLY|O_CREAT|O_EXCL": os.O_RDONLY | os.O_CREAT | os.O_EXCL,
}


def free_tcp_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class CreateOnlyOpenHandlerTest(unittest.TestCase):
    """ハンドラを直接呼ぶ（通信なし）"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-create-only-")

    def _handler(self, stopped):
        from core.sftp_server import SFTPServerHandler, _OpenWriters
        stop_event = threading.Event()
        if stopped:
            stop_event.set()
        self.writers = _OpenWriters()
        return SFTPServerHandler(None, self.root, open_writers=self.writers,
                                 stop_event=stop_event)

    def _open(self, handler, name, flags):
        import paramiko
        result = handler.open("/" + name, flags, paramiko.SFTPAttributes())
        if hasattr(result, "close"):
            self.addCleanup(result.close)
        return result

    def test_a_create_only_open_is_refused_after_stop(self):
        from paramiko import SFTP_FAILURE
        handler = self._handler(stopped=True)
        for label, flags in CREATE_ONLY.items():
            with self.subTest(flags=label):
                name = "new-%d.cfg" % flags
                self.assertEqual(self._open(handler, name, flags), SFTP_FAILURE,
                                 "停止の後に届いた %s を断っていない" % label)
                self.assertFalse(os.path.exists(os.path.join(self.root, name)),
                                 "停止の後に届いた %s が空ファイルを作った" % label)

    def test_a_create_only_open_still_creates_while_running(self):
        """対照: 停止していなければ、これまでどおり作って読み取りのハンドルを返す"""
        from paramiko import SFTP_FAILURE
        handler = self._handler(stopped=False)
        for label, flags in CREATE_ONLY.items():
            with self.subTest(flags=label):
                name = "new-%d.cfg" % flags
                result = self._open(handler, name, flags)
                self.assertNotEqual(result, SFTP_FAILURE)
                self.assertTrue(os.path.exists(os.path.join(self.root, name)))
                self.assertIsNone(getattr(result, "writefile", None),
                                  "作成だけの OPEN が書き込み可能なハンドルになった")
        self.assertEqual(self.writers.alive(), [],
                         "開き終えた作成だけの OPEN が停止の一覧に残っている")

    def test_a_create_only_open_is_not_refused_by_a_writer(self):
        """対照: 書き込みの予約は今までどおり。作成だけの OPEN は既存の
        ファイルを変えないので、別の接続が書き込み中でも断らない"""
        from paramiko import SFTP_FAILURE
        handler = self._handler(stopped=False)
        target = os.path.join(self.root, NEW)
        with open(target, "wb") as seed:
            seed.write(b"WRITING")
        reserved, release = threading.Event(), threading.Event()

        def writer():
            self.assertTrue(self.writers.reserve(os.path.realpath(target)))
            reserved.set()
            release.wait(10)

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(release.set)
        self.assertTrue(reserved.wait(10), "前提: 別のスレッドが予約していない")
        result = self._open(handler, NEW, os.O_RDONLY | os.O_CREAT)
        self.assertNotEqual(result, SFTP_FAILURE,
                            "書き込み中の保存先への作成だけの OPEN を断った")
        with open(target, "rb") as check:
            self.assertEqual(check.read(), b"WRITING")


class CreateOnlyOpenServerTest(unittest.TestCase):
    """実際の SFTP の通信で確かめる（127.0.0.1 のみ）"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import paramiko
        cls.host_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        home = mock.patch("core.config_manager.app_data_dir",
                          return_value=data_dir)
        home.start()
        self.addCleanup(home.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-create-only-")
        with open(os.path.join(self.root, SLOW), "wb") as handle:
            handle.write(b"x")
        self.port = free_tcp_port()
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)   # 止めた旧スレッドを解放してから後片付けへ
        self.entered = threading.Event()
        self.armed = {"on": False, "thread": None}
        self.results = []
        self.done = threading.Event()

    def _manager(self):
        from core.sftp_server import SFTPServerManager
        manager = SFTPServerManager()
        manager.host_key = self.host_key
        self.addCleanup(manager.stop)
        return manager

    def _start(self, manager):
        return manager.start(port=self.port, root_dir=self.root,
                             username=USER, password=PASSWORD)

    def _connect(self):
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect("127.0.0.1", port=self.port, username=USER,
                       password=PASSWORD, look_for_keys=False,
                       allow_agent=False, timeout=10)
        self.addCleanup(client.close)
        return client

    def _hold_stat_and_watch_open(self):
        """SLOW への STAT を 1 回だけ合図まで止め、同じスレッドの OPEN の戻り値を覚える"""
        from core.sftp_server import SFTPServerHandler
        real_stat, real_open = SFTPServerHandler.stat, SFTPServerHandler.open
        armed, gate, entered = self.armed, self.gate, self.entered
        results, done = self.results, self.done

        def stat(handler, path):
            if armed["on"] and path.endswith(SLOW):
                armed["on"] = False
                armed["thread"] = threading.current_thread()
                entered.set()
                gate.wait(30)
            return real_stat(handler, path)

        def watched_open(handler, *args):
            result = real_open(handler, *args)
            if threading.current_thread() is armed["thread"]:
                results.append(result)
                done.set()
            return result

        for name, func in (("stat", stat), ("open", watched_open)):
            patcher = mock.patch.object(SFTPServerHandler, name, func)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _check_queued_open_after_restart(self, pflags):
        import paramiko
        from paramiko import sftp as psftp
        self._hold_stat_and_watch_open()
        manager = self._manager()
        self.assertTrue(self._start(manager))
        client = self._connect()
        sftp = client.open_sftp()
        self.armed["on"] = True
        sftp._async_request(type(None), psftp.CMD_STAT, "/" + SLOW)
        self.assertTrue(self.entered.wait(20), "前提: STAT がサーバー側で止まっていない")
        # 応答を待たずに積む（パイプライン）。後ろの global request の応答が
        # 返れば、積んだ要求はサーバー側のチャネルのバッファへ届いている
        sftp._async_request(type(None), psftp.CMD_OPEN, "/" + NEW, pflags,
                            paramiko.SFTPAttributes())
        client.get_transport().global_request("sync@example.com", wait=True)

        manager.stop()
        self.assertTrue(self._start(manager),
                        "前提: 止まっているのは STAT だけなので、起動し直せるはず")
        self.gate.set()
        self.assertTrue(self.done.wait(15),
                        "前提: 積まれていた OPEN を旧スレッドが処理していない")
        self.assertEqual(self.results, [paramiko.SFTP_FAILURE],
                         "停止の前に積まれた作成の OPEN を断っていない")
        self.assertFalse(os.path.exists(os.path.join(self.root, NEW)),
                         "停止の前に積まれた作成の OPEN が、再起動の後に空ファイルを作った")

    def test_a_queued_create_open_does_not_run_after_the_restart(self):
        from paramiko import sftp as psftp
        self._check_queued_open_after_restart(psftp.SFTP_FLAG_CREATE)

    def test_a_queued_exclusive_create_open_does_not_run_after_the_restart(self):
        """paramiko のクライアントの open(name, 'x') と同じフラグ"""
        from paramiko import sftp as psftp
        self._check_queued_open_after_restart(
            psftp.SFTP_FLAG_CREATE | psftp.SFTP_FLAG_EXCL)

    def test_start_is_refused_while_a_create_only_open_is_stuck(self):
        real_open = os.open
        gate, entered = self.gate, self.entered

        def blocking(path, flags, *args, **kwargs):
            if str(path).endswith(NEW):
                entered.set()
                gate.wait(30)
            return real_open(path, flags, *args, **kwargs)

        manager = self._manager()
        self.assertTrue(self._start(manager))
        sftp = self._connect().open_sftp()
        patcher = mock.patch.object(os, "open", blocking)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 後片付けの停止より先に、止めた旧スレッドを解放する
        self.addCleanup(self.gate.set)

        def send():
            try:
                sftp.open(NEW, "x")      # O_RDONLY|O_CREAT|O_EXCL で届く
            except Exception:
                pass                     # 停止で切られるのは想定どおり

        threading.Thread(target=send, daemon=True).start()
        self.assertTrue(self.entered.wait(20),
                        "前提: 作成の OPEN がサーバー側で止まっていない")
        self.assertIs(manager.stop(), False,
                      "stop() が止まった作成の OPEN を生き残りとして数えていない")
        self.assertFalse(self._start(manager),
                         "止まった作成の OPEN が残っているのに起動を受け付けている")


if __name__ == "__main__":
    unittest.main()
