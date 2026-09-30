"""srv-01 の残り: 削除・改名・切り詰めの最中に止まった旧スレッドを、停止が数えない件。

何が起きていたか（実測、基準 441ea02。127.0.0.1 のみ）: 1.3.1 で、停止の
期限を過ぎても書き込みを抱えたスレッドが残っている間は、次の start() を
「前回の停止が完了していません」と断るようにした。ただし数えていたのは
open() が os.open の後で一覧（_OpenWriters）へ載せた書き込みハンドルだけで、
保存先を触っている別の操作は見ていなかった。共有フォルダの遅延・切断を
模して、サーバ側の os.remove / os.rename を合図まで止めると:

  remove: stop() は 0.5 秒で True を返し、start() も True。新しい起動で
          受けた同名のアップロード b'NEW-CONTENT-NEW-CONTENT' が、後から
          戻った旧 os.remove に消された（FILE GONE）
  rename: 同じく再起動が通り、旧 os.rename（config.cfg → backup.cfg）が
          戻ると config.cfg が消え、新しい中身が backup.cfg へ移った
  open（O_TRUNC）・truncate: 同じく stop() が True・再起動も通った
          （1.3.1 の予約が再起動をまたいで残るので、同名のアップロードは
          断られて中身は壊れなかったが、停止の時点では数えていなかった）

機器の config を受け取る道具なので、新しく受けたファイルが黙って消える・
別名へ動くのは許容できない。

どう直したか: _OpenWriters に「保存先を触っている最中」を載せる busy() を
足し、remove（対象）・rename（元と先の両方）・open の os.open・SETSTAT の
os.truncate をその中で行う。止まったスレッドは書き込みハンドルと同じく
一覧に残るので、stop() が最大 1 秒待ってから生き残りとして覚え、次の
start() を断る。操作が終われば一覧から外れ、これまでどおり起動できる。
"""
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

USER = "tester"
PASSWORD = "example-pass"
OLD = b"OLD-CONTENT"
NEW = b"NEW-CONTENT-NEW-CONTENT"
TARGET = "config.cfg"


def free_tcp_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _OsProxy(types.ModuleType):
    """core.sftp_server から見える os。差し替えた関数以外は本物へ渡す"""

    def __getattr__(self, name):
        return getattr(os, name)


class SftpRestartAfterStuckRemoveRenameTest(unittest.TestCase):
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
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-stuck-op-")
        self.port = free_tcp_port()
        self.gate = threading.Event()       # 止めた操作を通す合図
        self.entered = threading.Event()    # 操作で止まった合図
        self.armed = {"op": None}           # 止める os の関数名

    def tearDown(self):
        self.gate.set()   # 生き残りを解放してから後片付けへ入る

    def _block(self, op):
        """core.sftp_server の os.<op> を、TARGET に対してだけ合図まで止める。

        共有フォルダの遅延・切断で、サーバ側の削除・改名などが長く掛かる
        状況を模す。止めるのは armed の 1 回だけ
        """
        import core.sftp_server as sftp_server
        real = getattr(os, op)
        armed, gate, entered = self.armed, self.gate, self.entered

        def blocking(*args, **kwargs):
            if armed["op"] == op and str(args[0]).endswith(TARGET):
                armed["op"] = None
                entered.set()
                gate.wait(30)
            return real(*args, **kwargs)

        proxy = _OsProxy("os_proxy")
        setattr(proxy, op, blocking)
        patcher = mock.patch.object(sftp_server, "os", proxy)
        patcher.start()
        self.addCleanup(patcher.stop)

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
        return client

    def _upload(self, payload):
        client = self._connect()
        try:
            sftp = client.open_sftp()
            with sftp.open(TARGET, "wb") as remote:
                remote.write(payload)
        finally:
            client.close()

    def _leave_an_operation_stuck(self, manager, op):
        """旧クライアントの op を 1 件走らせ、サーバ側の os.<op> で止める"""
        self._block(op)
        self.assertTrue(self._start(manager))
        if op in ("remove", "rename", "truncate"):
            self._upload(OLD)   # 消す・動かす・切り詰める対象を先に置く

        def send():
            try:
                client = self._connect()
                sftp = client.open_sftp()
                if op == "open":
                    with sftp.open(TARGET, "wb") as remote:
                        remote.write(OLD)
                elif op == "remove":
                    sftp.remove(TARGET)
                elif op == "rename":
                    sftp.rename(TARGET, "backup.cfg")
                elif op == "truncate":
                    sftp.truncate(TARGET, 0)
            except Exception:
                pass   # 停止で切られるのは想定どおり

        self.armed["op"] = op
        threading.Thread(target=send, daemon=True).start()
        self.assertTrue(self.entered.wait(20),
                        "os.%s() まで進んでいない（前提が崩れている）" % op)

    def _assert_restart_refused(self, op):
        manager = self._manager()
        self._leave_an_operation_stuck(manager, op)

        finished = manager.stop()
        errors = []
        manager.error_occurred.connect(errors.append)
        started = self._start(manager)

        self.assertFalse(
            started,
            "os.%s() で止まった旧スレッドが残っているのに起動を受け付けている"
            % op)
        from core.sftp_server import PREVIOUS_STOP_INCOMPLETE_MESSAGE
        self.assertIn(PREVIOUS_STOP_INCOMPLETE_MESSAGE, errors,
                      "断った理由を伝えていない: %r" % (errors,))
        self.assertIs(finished, False,
                      "stop() が os.%s() の生き残りを知らせていない" % op)
        return manager

    def _restart_after_release(self, manager):
        self.gate.set()   # 止めていた旧操作を通す
        deadline = time.time() + 15
        while time.time() < deadline:
            if self._start(manager):
                return
            time.sleep(0.1)
        self.fail("旧操作が終わっても起動できないままになっている")

    def _read(self, name):
        path = os.path.join(self.root, name)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as handle:
            return handle.read()

    # --- 本題 ------------------------------------------------------------

    def test_start_is_refused_while_a_stuck_remove_survives(self):
        self._assert_restart_refused("remove")

    def test_start_is_refused_while_a_stuck_rename_survives(self):
        self._assert_restart_refused("rename")

    def test_start_is_refused_while_a_stuck_truncating_open_survives(self):
        self._assert_restart_refused("open")

    def test_start_is_refused_while_a_stuck_truncate_survives(self):
        self._assert_restart_refused("truncate")

    def test_a_new_upload_survives_the_old_remove(self):
        """旧 remove が終わってから起動するので、新しいアップロードが消えない"""
        manager = self._assert_restart_refused("remove")
        self._restart_after_release(manager)

        self._upload(NEW)
        time.sleep(0.5)   # 旧スレッドの後追いがあれば、ここまでに起きる
        self.assertEqual(self._read(TARGET), NEW,
                         "新しい起動で受けたアップロードが旧 remove に消された")

    def test_a_new_upload_survives_the_old_rename(self):
        """旧 rename が終わってから起動するので、新しい中身が別名へ動かない"""
        manager = self._assert_restart_refused("rename")
        self._restart_after_release(manager)

        self._upload(NEW)
        time.sleep(0.5)
        self.assertEqual(self._read(TARGET), NEW,
                         "新しい起動で受けたアップロードが旧 rename に動かされた")
        self.assertEqual(self._read("backup.cfg"), OLD,
                         "旧 rename は旧い中身だけを動かすはず")

    def test_a_finished_remove_does_not_block_the_restart(self):
        """対照: 止まらずに終わった削除は、停止・起動を妨げない"""
        manager = self._manager()
        self.assertTrue(self._start(manager))
        self._upload(OLD)
        client = self._connect()
        try:
            client.open_sftp().remove(TARGET)
        finally:
            client.close()

        finished = manager.stop()
        self.assertIs(finished, True,
                      "終わった削除を生き残りとして数えている")
        self.assertTrue(self._start(manager), "普通に停止した後に起動できない")


if __name__ == "__main__":
    unittest.main()
