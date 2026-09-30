"""srv-01 の残り（検査役の指摘）: 止まった STAT の後ろに積まれた要求が、再起動の後で実行される件。

何が起きていたか（実測、11191e7。127.0.0.1 のみ）: 停止が数えるのは、書き込み・
削除・改名・切り詰めの最中のスレッドだけ（_OpenWriters.busy）。STAT・一覧・
読み取りの OPEN はどれも書かないので数えない。ところが SFTP のクライアントは
応答を待たずに要求を積める。paramiko の Channel.recv は閉じた後も受信バッファに
残った分を返し、SFTPServer.start_subsystem は応答を送れなくても次のパケットを
読み続ける。そのため共有フォルダの遅延で STAT が止まっている間に REMOVE を
積まれると:

  stop() は 0.5 秒で True、start() も True（STAT は数えない）
  新しい起動で同じ名前のアップロードを受ける
  止まっていた STAT が戻ると、積まれていた旧 REMOVE がそれを消した
  （OPEN(WRITE|CREAT|TRUNC) なら 0 バイトに切り詰めた。RENAME なら別名へ動かした）

どれも元の件と同じ「新しく受けた config が黙って消える・空になる・別名へ動く」。
441ea02 でも同じ（回帰ではない）。

どう直したか: ハンドラへ、その起動の停止フラグ（_handle_client が起動ごとの
_stop_event を渡す）を持たせた。_OpenWriters.busy は一覧へ載せた後に停止フラグを
見て、立っていれば断る（SFTP_FAILURE）。stop() はフラグを立ててから一覧を読むので、
「載せる→フラグを見る」の順なら、どちらが先でも停止に数えられるか、ここで断られる。
あわせて mkdir・rmdir も busy の中で行う。
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
OLD = b"OLD-CONTENT"
NEW = b"NEW-CONTENT-NEW-CONTENT"
TARGET = "config.cfg"
SLOW = "slow.cfg"


def free_tcp_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class SftpPipelinedRequestAfterStopTest(unittest.TestCase):
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
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-pipelined-")
        with open(os.path.join(self.root, SLOW), "wb") as handle:
            handle.write(b"x")
        self.port = free_tcp_port()
        self.gate = threading.Event()       # 止めた STAT を通す合図
        self.entered = threading.Event()    # STAT で止まった合図
        self.armed = {"on": False, "thread": None}
        self.results = []                   # 旧スレッドが積まれた要求に返した値
        self.done = threading.Event()       # 旧スレッドが積まれた要求を処理した合図

    def tearDown(self):
        self.gate.set()   # 止めた旧スレッドを解放してから後片付けへ入る

    def _hold_stat(self):
        """SLOW への STAT を 1 回だけ合図まで止める（共有フォルダの遅延を模す）"""
        from core.sftp_server import SFTPServerHandler
        real = SFTPServerHandler.stat
        armed, gate, entered = self.armed, self.gate, self.entered

        def stat(handler, path):
            if armed["on"] and path.endswith(SLOW):
                armed["on"] = False
                armed["thread"] = threading.current_thread()
                entered.set()
                gate.wait(30)
            return real(handler, path)

        patcher = mock.patch.object(SFTPServerHandler, "stat", stat)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _watch(self, method):
        """止まった旧スレッドが method を処理したら、戻り値を覚えて合図する"""
        from core.sftp_server import SFTPServerHandler
        real = getattr(SFTPServerHandler, method)
        armed, results, done = self.armed, self.results, self.done

        def watched(handler, *args):
            result = real(handler, *args)
            if threading.current_thread() is armed["thread"]:
                results.append(result)
                done.set()
            return result

        patcher = mock.patch.object(SFTPServerHandler, method, watched)
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
        self.addCleanup(client.close)
        return client

    def _upload(self, payload):
        client = self._connect()
        try:
            sftp = client.open_sftp()
            with sftp.open(TARGET, "wb") as remote:
                remote.write(payload)
        finally:
            client.close()

    def _queue_behind_a_stuck_stat(self, manager, method, command, *args):
        """旧クライアントの STAT をサーバー側で止め、その後ろへ要求を 1 件積む。

        積んだ要求は応答を待たない（パイプライン）。サーバーの Transport は
        受けた順に処理するので、後ろに送った global request の応答が返れば、
        積んだ要求はサーバー側のチャネルのバッファへ届いている
        """
        from paramiko import sftp as psftp
        self._hold_stat()
        self._watch(method)
        self.assertTrue(self._start(manager))
        self._upload(OLD)

        client = self._connect()
        sftp = client.open_sftp()
        self.armed["on"] = True
        sftp._async_request(type(None), psftp.CMD_STAT, "/" + SLOW)
        self.assertTrue(self.entered.wait(20),
                        "前提: STAT がサーバー側で止まっていない")
        sftp._async_request(type(None), command, *args)
        client.get_transport().global_request("sync@example.com", wait=True)

    def _restart_then_release(self, manager, prepare=None):
        """停止→起動し直し→新しい起動で同名を受けてから、止めた STAT を通す"""
        manager.stop()
        self.assertTrue(
            self._start(manager),
            "前提: 止まっているのは STAT だけなので、起動し直せるはず")
        self._upload(NEW)
        if prepare is not None:
            prepare()
        self.gate.set()
        self.assertTrue(self.done.wait(15),
                        "前提: 積まれていた要求を旧スレッドが処理していない")

    def _read(self, name):
        path = os.path.join(self.root, name)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as handle:
            return handle.read()

    def _attrs(self, **fields):
        import paramiko
        attr = paramiko.SFTPAttributes()
        for name, value in fields.items():
            setattr(attr, name, value)
        return attr

    # --- 本題 ------------------------------------------------------------

    def test_a_queued_remove_does_not_delete_the_new_upload(self):
        from paramiko import sftp as psftp
        manager = self._manager()
        self._queue_behind_a_stuck_stat(manager, "remove", psftp.CMD_REMOVE,
                                        "/" + TARGET)
        self._restart_then_release(manager)

        self.assertEqual(self._read(TARGET), NEW,
                         "停止の前に積まれた REMOVE が、再起動の後に受けた"
                         "アップロードを消した")

    def test_a_queued_truncating_open_does_not_empty_the_new_upload(self):
        from paramiko import sftp as psftp
        manager = self._manager()
        flags = (psftp.SFTP_FLAG_WRITE | psftp.SFTP_FLAG_CREATE
                 | psftp.SFTP_FLAG_TRUNC)
        self._queue_behind_a_stuck_stat(manager, "open", psftp.CMD_OPEN,
                                        "/" + TARGET, flags, self._attrs())
        self._restart_then_release(manager)

        self.assertEqual(self._read(TARGET), NEW,
                         "停止の前に積まれた OPEN(TRUNC) が、再起動の後に"
                         "受けたアップロードを切り詰めた")

    def test_a_queued_rename_does_not_move_the_new_upload(self):
        from paramiko import sftp as psftp
        manager = self._manager()
        self._queue_behind_a_stuck_stat(manager, "rename", psftp.CMD_RENAME,
                                        "/" + TARGET, "/backup.cfg")
        self._restart_then_release(manager)

        self.assertEqual(self._read(TARGET), NEW,
                         "停止の前に積まれた RENAME が、再起動の後に受けた"
                         "アップロードを別名へ動かした")
        self.assertIsNone(self._read("backup.cfg"))

    def test_a_queued_setstat_does_not_truncate_the_new_upload(self):
        from paramiko import sftp as psftp
        manager = self._manager()
        self._queue_behind_a_stuck_stat(manager, "chattr", psftp.CMD_SETSTAT,
                                        "/" + TARGET, self._attrs(st_size=0))
        self._restart_then_release(manager)

        self.assertEqual(self._read(TARGET), NEW,
                         "停止の前に積まれた SETSTAT が、再起動の後に受けた"
                         "アップロードを切り詰めた")

    def test_a_queued_mkdir_does_not_run_after_the_restart(self):
        from paramiko import sftp as psftp
        manager = self._manager()
        self._queue_behind_a_stuck_stat(manager, "mkdir", psftp.CMD_MKDIR,
                                        "/newdir", self._attrs())
        self._restart_then_release(manager)

        self.assertFalse(os.path.exists(os.path.join(self.root, "newdir")),
                         "停止の前に積まれた MKDIR が、再起動の後に実行された")

    def test_a_queued_rmdir_does_not_remove_a_directory_of_the_new_start(self):
        from paramiko import sftp as psftp
        manager = self._manager()
        keep = os.path.join(self.root, "keep")
        self._queue_behind_a_stuck_stat(manager, "rmdir", psftp.CMD_RMDIR,
                                        "/keep")
        # 新しい起動で空のディレクトリができた後に、旧 RMDIR を通す
        self._restart_then_release(manager, prepare=lambda: os.mkdir(keep))

        self.assertTrue(os.path.isdir(keep),
                        "停止の前に積まれた RMDIR が、再起動の後にできた"
                        "ディレクトリを消した")

    def test_the_queued_request_is_answered_with_a_failure(self):
        from paramiko import SFTP_FAILURE
        from paramiko import sftp as psftp
        manager = self._manager()
        self._queue_behind_a_stuck_stat(manager, "remove", psftp.CMD_REMOVE,
                                        "/" + TARGET)
        self._restart_then_release(manager)

        self.assertEqual(self.results, [SFTP_FAILURE])

    def _assert_a_stuck_call_blocks_the_restart(self, name, send):
        """os.<name> を止めたまま停止すると、生き残りとして数えて起動を断る"""
        real = getattr(os, name)
        gate, entered = self.gate, self.entered

        def blocking(path, *args, **kwargs):
            if str(path).endswith("slowdir"):
                entered.set()
                gate.wait(30)
            return real(path, *args, **kwargs)

        patcher = mock.patch.object(os, name, blocking)
        manager = self._manager()
        self.assertTrue(self._start(manager))
        sftp = self._connect().open_sftp()
        patcher.start()
        self.addCleanup(patcher.stop)
        threading.Thread(target=lambda: self._quietly(send, sftp),
                         daemon=True).start()
        self.assertTrue(self.entered.wait(20),
                        "前提: %s がサーバー側で止まっていない" % name)

        finished = manager.stop()
        self.assertIs(finished, False,
                      "stop() が止まった %s を生き残りとして数えていない" % name)
        self.assertFalse(self._start(manager),
                         "止まった %s が残っているのに起動を受け付けている"
                         % name)

    @staticmethod
    def _quietly(send, sftp):
        try:
            send(sftp)
        except Exception:
            pass   # 停止で切られるのは想定どおり

    def test_start_is_refused_while_a_mkdir_is_stuck(self):
        self._assert_a_stuck_call_blocks_the_restart(
            "mkdir", lambda sftp: sftp.mkdir("slowdir"))

    def test_start_is_refused_while_a_rmdir_is_stuck(self):
        os.mkdir(os.path.join(self.root, "slowdir"))
        self._assert_a_stuck_call_blocks_the_restart(
            "rmdir", lambda sftp: sftp.rmdir("slowdir"))

    def test_a_queued_remove_still_runs_while_the_server_keeps_running(self):
        """対照: 停止していなければ、積まれた要求はこれまでどおり処理される"""
        from paramiko import SFTP_OK
        from paramiko import sftp as psftp
        manager = self._manager()
        self._queue_behind_a_stuck_stat(manager, "remove", psftp.CMD_REMOVE,
                                        "/" + TARGET)
        self.gate.set()
        self.assertTrue(self.done.wait(15),
                        "前提: 積まれていた要求を処理していない")

        self.assertEqual(self.results, [SFTP_OK])
        self.assertIsNone(self._read(TARGET),
                          "停止していないのに、積まれた REMOVE を断った")


if __name__ == "__main__":
    unittest.main()
