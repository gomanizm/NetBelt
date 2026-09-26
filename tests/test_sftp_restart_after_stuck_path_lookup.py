"""srv-01 の残り（検査役の指摘）: 保存先のパスを解決している最中に止まった旧スレッドを、停止が数えない件。

何が起きていたか（実測、aa8b35a。127.0.0.1 のみ）: 110d6f9 で、削除・改名・
切り詰めの最中のスレッドも停止に数えるようにした（_OpenWriters.busy）。
ただし一覧へ載せていたのは最後の os.remove / os.rename / os.open /
os.truncate を呼ぶ間だけで、その前のパスの解決（_get_link_path・
_get_real_path の os.path.realpath）の間は載っていなかった。共有フォルダの
遅延・切断を模して、そこを合図まで止めると:

  REMOVE を _get_link_path で止める: stop() は 0.5 秒で True、start() も
          True。新しい起動で受けた同名のアップロードが、後から戻った旧
          os.remove に消された
  OPEN 'wb' を os.path.realpath で止める: 同じく再起動が通り、新しい
          アップロードの中身が、後から戻った旧 os.open の O_TRUNC で 0 バイトに
          切り詰められた
  SETSTAT の chmod で止める: 同じく再起動が通り、後から戻った旧 chmod が
          新しいアップロードを読み取り専用にした

どれも元の件と同じ「新しく受けた config が黙って消える・空になる・変わる」。

どう直したか: 一覧へ載せる区間を、ハンドラの操作の入口（パスを解決する前）
からに広げた。remove・rename は本体をまるごと、SETSTAT（chattr）も本体を
まるごと busy の中で行う。open は書き込みか切り詰めを伴うかがフラグだけで
決まるので、そのときはパスを解決する前から載せる。
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
TARGET = "config.cfg"


def free_tcp_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class SftpRestartAfterStuckPathLookupTest(unittest.TestCase):
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
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-stuck-lookup-")
        self.port = free_tcp_port()
        self.gate = threading.Event()       # 止めた処理を通す合図
        self.entered = threading.Event()    # 処理で止まった合図
        self.armed = {"op": None}           # 止める要求の種類

    def tearDown(self):
        self.gate.set()   # 生き残りを解放してから後片付けへ入る

    def _blocking(self, op, real, method=False):
        """real を、TARGET に対して armed の 1 回だけ合図まで止める版にする"""
        armed, gate, entered = self.armed, self.gate, self.entered

        def blocking(*args, **kwargs):
            path = args[1] if method else args[0]
            if armed["op"] == op and str(path).endswith(TARGET):
                armed["op"] = None
                entered.set()
                gate.wait(30)
            return real(*args, **kwargs)

        return blocking

    def _block(self, op):
        """op の要求を、保存先を実際に触る前の段で止める。

        remove / rename: _get_link_path（親ディレクトリの realpath）
        open / truncate: _get_real_path の中の os.path.realpath
        chmod: SETSTAT の os.chmod（切り詰め・日時より後の段）
        """
        from core.sftp_server import SFTPServerHandler
        if op in ("remove", "rename"):
            patcher = mock.patch.object(
                SFTPServerHandler, "_get_link_path",
                self._blocking(op, SFTPServerHandler._get_link_path,
                               method=True))
        elif op == "chmod":
            patcher = mock.patch.object(
                os, "chmod", self._blocking(op, os.chmod))
        else:
            patcher = mock.patch.object(
                os.path, "realpath", self._blocking(op, os.path.realpath))
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
        """旧クライアントの op を 1 件走らせ、パスの解決などで止める"""
        self._block(op)
        self.assertTrue(self._start(manager))
        if op != "open":
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
                elif op == "chmod":
                    sftp.chmod(TARGET, 0o444)
            except Exception:
                pass   # 停止で切られるのは想定どおり

        self.armed["op"] = op
        threading.Thread(target=send, daemon=True).start()
        self.assertTrue(self.entered.wait(20),
                        "%s の要求が止める段まで進んでいない（前提が崩れている）"
                        % op)

    def _assert_restart_refused(self, op):
        manager = self._manager()
        self._leave_an_operation_stuck(manager, op)

        finished = manager.stop()
        errors = []
        manager.error_occurred.connect(errors.append)
        started = self._start(manager)

        self.assertFalse(
            started,
            "%s の要求がパスの解決などで止まったまま残っているのに、"
            "起動を受け付けている" % op)
        from core.sftp_server import PREVIOUS_STOP_INCOMPLETE_MESSAGE
        self.assertIn(PREVIOUS_STOP_INCOMPLETE_MESSAGE, errors,
                      "断った理由を伝えていない: %r" % (errors,))
        self.assertIs(finished, False,
                      "stop() が %s の生き残りを知らせていない" % op)
        return manager

    def _restart_after_release(self, manager):
        self.gate.set()   # 止めていた旧処理を通す
        deadline = time.time() + 15
        while time.time() < deadline:
            if self._start(manager):
                return
            time.sleep(0.1)
        self.fail("旧処理が終わっても起動できないままになっている")

    def _read(self, name):
        path = os.path.join(self.root, name)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as handle:
            return handle.read()

    # --- 本題 ------------------------------------------------------------

    def test_start_is_refused_while_a_remove_is_stuck_resolving_the_path(self):
        self._assert_restart_refused("remove")

    def test_start_is_refused_while_a_rename_is_stuck_resolving_the_path(self):
        self._assert_restart_refused("rename")

    def test_start_is_refused_while_a_truncating_open_is_stuck_in_realpath(self):
        self._assert_restart_refused("open")

    def test_start_is_refused_while_a_truncate_is_stuck_in_realpath(self):
        self._assert_restart_refused("truncate")

    def test_start_is_refused_while_a_setstat_chmod_is_stuck(self):
        self._assert_restart_refused("chmod")

    def test_a_new_upload_survives_the_old_remove_after_the_lookup(self):
        """旧 remove が終わってから起動するので、新しいアップロードが消えない"""
        manager = self._assert_restart_refused("remove")
        self._restart_after_release(manager)

        self._upload(NEW)
        time.sleep(0.5)   # 旧スレッドの後追いがあれば、ここまでに起きる
        self.assertEqual(self._read(TARGET), NEW,
                         "新しい起動で受けたアップロードが旧 remove に消された")

    def test_a_new_upload_survives_the_old_truncating_open(self):
        """旧 open が終わってから起動するので、新しい中身が切り詰められない"""
        manager = self._assert_restart_refused("open")
        self._restart_after_release(manager)

        self._upload(NEW)
        time.sleep(0.5)
        self.assertEqual(self._read(TARGET), NEW,
                         "新しい起動で受けたアップロードが旧 open に切り詰められた")

    def test_a_finished_setstat_does_not_block_the_restart(self):
        """対照: 止まらずに終わった SETSTAT は、停止・起動を妨げない"""
        manager = self._manager()
        self.assertTrue(self._start(manager))
        self._upload(OLD)
        client = self._connect()
        try:
            client.open_sftp().utime(TARGET, (1000000000, 1000000000))
        finally:
            client.close()

        finished = manager.stop()
        self.assertIs(finished, True,
                      "終わった SETSTAT を生き残りとして数えている")
        self.assertTrue(self._start(manager), "普通に停止した後に起動できない")


if __name__ == "__main__":
    unittest.main()
