"""置き換えの途中のアップロードが、タブを閉じる・機器側の切断・アプリの終了で取り消されないことを検証する。

何が起きていたか（afb8abc の後、localhost で実測）。SSH の後始末で GUI が
止まらないように、MainWindow の後始末を「SSH を先に閉じ、SFTP は後」へ
入れ替えた。ところが SFTPManager.disconnect は進行中の転送が手を離すのを
最大 3 秒待つ作りで、その待ちより先に SSH の Transport を閉じるように
なったため、置き換えの途中のアップロードが断ち切られた。

アップロードは一時名へ送ってから最終名へ置き換える（posix_rename、無ければ
rename → 失敗なら最終名を remove → rename）。進捗が 100% に見えたあとも、
一時名の確認（stat）と置き換えの往復が残る。その間にタブを閉じると:
  - posix_rename あり: 機器の cfg.txt は旧版のまま、一時名が残る
  - posix_rename 無しで、機器が最終名を remove した直後: cfg.txt が消え、
    一時名だけが残る
知らせはコンソールの print だけで、画面には何も出ない（パネルは空にされ、
現在の接続でない SFTP のエラーは捨てられる）。441ea02 の順序（SFTP を先に
閉じる）では、待つ間に置き換えが終わり cfg.txt は新しい内容になっていた。

どう直したか。後始末では SSH を閉じる前に SFTPManager.quiesce() を呼ぶ。
新しい操作を止め、進行中の操作が手を離すのを _DISCONNECT_WAIT_SECONDS まで
待つ（何も書かない）。そのあと SSH を閉じ、最後に SFTP のクライアントを閉じる。
相手が TCP まで詰まっていても、止まるのは待ちの上限まで。

機器はメモリ上のファイルを見せる localhost の paramiko。置き換えの途中
（一時名の確認か、最終名の remove の直後）で合図を出し、応答を PAUSE_SECONDS
遅らせる。テストはその合図を見てから後始末を始める。
"""
import json
import os
import posixpath
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import paramiko
from paramiko import (SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface,
                      SFTP_FAILURE, SFTP_NO_SUCH_FILE, SFTP_OK, SFTP_OP_UNSUPPORTED)

sys.path.insert(0, "src")

FINAL = "/cfg.txt"
OLD = b"hostname old.example.com\n"
NEW = b"hostname new.example.com\n" * 4000     # 約 100KB
PAUSE_SECONDS = 1.0     # 置き換えの途中で機器が応答を遅らせる時間


class _Device:
    """機器側のファイル（メモリ上）と、置き換えの途中で止まる位置"""

    def __init__(self, posix_rename: bool, pause_at: str,
                 close_shell_at_pause: bool = False):
        self.lock = threading.Lock()
        self.files = {FINAL: bytearray(OLD)}
        self.posix_rename = posix_rename
        self.pause_at = pause_at            # "confirm_stat" か "remove_final"
        self.close_shell_at_pause = close_shell_at_pause
        self.reached = threading.Event()
        self.shell_channel = None

    def attrs(self, path):
        a = SFTPAttributes()
        a.st_mode = stat.S_IFREG | 0o644
        a.st_size = len(self.files[path])
        a.st_uid = a.st_gid = 1000
        a.st_atime = a.st_mtime = 0
        a.filename = posixpath.basename(path)
        return a

    def pause(self):
        """置き換えの途中で合図を出し、応答を遅らせる"""
        self.reached.set()
        if self.close_shell_at_pause and self.shell_channel is not None:
            # 機器側がシェルだけを閉じる（Transport と SFTP は生きている）
            self.shell_channel.close()
        time.sleep(PAUSE_SECONDS)

    def snapshot(self):
        with self.lock:
            return {p: bytes(d) for p, d in self.files.items()}


class _Handle(SFTPHandle):
    def __init__(self, device, path, flags):
        super().__init__(flags)
        self.device = device
        self.path = path

    def write(self, offset, data):
        with self.device.lock:
            buf = self.device.files.get(self.path)
            if buf is None:
                return SFTP_FAILURE
            if len(buf) < offset:
                buf.extend(b"\0" * (offset - len(buf)))
            buf[offset:offset + len(data)] = data
        return SFTP_OK

    def stat(self):
        with self.device.lock:
            return self.device.attrs(self.path)

    def chattr(self, attr):
        return SFTP_OK


class _DeviceSFTP(SFTPServerInterface):
    def __init__(self, server, device):
        super().__init__(server)
        self.device = device

    def canonicalize(self, path):
        return posixpath.normpath(posixpath.join("/", path))

    def list_folder(self, path):
        d = self.canonicalize(path)
        with self.device.lock:
            return [self.device.attrs(p) for p in self.device.files
                    if posixpath.dirname(p) == d]

    def _stat(self, path):
        p = self.canonicalize(path)
        with self.device.lock:
            if p == "/":
                a = SFTPAttributes()
                a.st_mode = stat.S_IFDIR | 0o755
                return a
            if p not in self.device.files:
                return SFTP_NO_SUCH_FILE
            return self.device.attrs(p)

    def stat(self, path):
        result = self._stat(path)
        # put() が送り終えたあとの一時名の確認。ここから先が置き換え
        if (".netbelt-part." in path and self.device.pause_at == "confirm_stat"
                and not self.device.reached.is_set()):
            self.device.pause()
        return result

    def lstat(self, path):
        return self._stat(path)

    def open(self, path, flags, attr):
        p = self.canonicalize(path)
        with self.device.lock:
            exists = p in self.device.files
            if exists and flags & os.O_EXCL:
                return SFTP_FAILURE
            if not exists and not flags & os.O_CREAT:
                return SFTP_NO_SUCH_FILE
            if not exists or flags & os.O_TRUNC:
                self.device.files[p] = bytearray()
        return _Handle(self.device, p, flags)

    def remove(self, path):
        p = self.canonicalize(path)
        with self.device.lock:
            if p not in self.device.files:
                return SFTP_NO_SUCH_FILE
            del self.device.files[p]
        if p == FINAL and self.device.pause_at == "remove_final":
            self.device.pause()
        return SFTP_OK

    def rename(self, oldpath, newpath):
        old, new = self.canonicalize(oldpath), self.canonicalize(newpath)
        with self.device.lock:
            if new in self.device.files:
                return SFTP_FAILURE
            if old not in self.device.files:
                return SFTP_NO_SUCH_FILE
            self.device.files[new] = self.device.files.pop(old)
        return SFTP_OK

    def posix_rename(self, oldpath, newpath):
        if not self.device.posix_rename:
            return SFTP_OP_UNSUPPORTED
        old, new = self.canonicalize(oldpath), self.canonicalize(newpath)
        with self.device.lock:
            if old not in self.device.files:
                return SFTP_NO_SUCH_FILE
            self.device.files[new] = self.device.files.pop(old)
        return SFTP_OK

    def chattr(self, path, attr):
        return SFTP_OK


class _Shell(paramiko.ServerInterface):
    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        return True

    def check_channel_window_change_request(self, *args):
        return True


class _SSHDevice:
    """シェルと SFTP を持つ localhost の機器"""

    def __init__(self, device):
        self.device = device
        self.transport = None
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            accepted, _ = self.sock.accept()
        except OSError:
            return
        t = paramiko.Transport(accepted)
        self.transport = t
        t.add_server_key(paramiko.ECDSAKey.generate())
        t.set_subsystem_handler("sftp", SFTPServer, _DeviceSFTP, self.device)
        try:
            t.start_server(server=_Shell())
        except Exception:
            return
        ch = t.accept(10)
        if ch is None:
            return
        self.device.shell_channel = ch
        ch.sendall(b"sw1# ")
        ch.settimeout(0.2)
        while True:
            try:
                if not ch.recv(65536):
                    break
            except socket.timeout:
                continue
            except Exception:
                break

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        if self.transport is not None:
            self.transport.close()


class UploadSurvivesTeardownTest(unittest.TestCase):
    """置き換えの途中で後始末が始まっても、最終名は新しい内容になる"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-upload-teardown-"))
        self.local = self.dir / "new.txt"
        self.local.write_bytes(NEW)
        self.window_closed = False

    def _pump(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end and not (until and until()):
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    def _open_window(self, device):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        server = _SSHDevice(device)
        self.addCleanup(server.close)
        config_path = self.dir / "config.json"
        config_path.write_text(json.dumps({
            "groups": [{"name": "Default", "devices": [{
                "name": "sw1", "host": "127.0.0.1", "port": server.port,
                "protocol": "ssh", "username": "admin", "password": "pw"}]}],
            "global_macros": []}), encoding="utf-8")
        for patcher in (
                mock.patch("core.config_manager.app_data_dir",
                           return_value=self.dir),
                mock.patch("ui.main_window.ConfigManager",
                           lambda *a, **k: ConfigManager(str(config_path))),
                mock.patch("ui.main_window.MainWindow._check_for_updates_on_startup",
                           lambda self: None),
                mock.patch("ui.sftp_panel.QMessageBox"),
                mock.patch("ui.main_window.QMessageBox")):
            patcher.start()
            self.addCleanup(patcher.stop)
        window = MainWindow()
        self.addCleanup(self._close_window, window)
        window._on_connect_requested(
            window.config_manager.get_groups()[0]["devices"][0])
        self._pump(20, until=lambda: "sw1" in window.sftp_managers)
        self.assertIn("sw1", window.sftp_managers, "前提: SFTP が開いていない")
        # 終了処理の MIB 読み込み待ちで、閉じるのが遅れないようにしておく
        window.snmp_panel.wait_for_background_work()
        self._pump(0.3)
        return window

    def _close_window(self, window):
        if self.window_closed:
            return
        for name in list(window.connections):
            window._dispose_connection(name)
        with mock.patch("sys.excepthook", lambda *args: None):
            window.close()

    def _upload_until_paused(self, window, device):
        """置き換えの途中（機器の合図）まで進め、そのとき動いている転送スレッドを返す"""
        mgr = window.sftp_managers["sw1"]
        before = set(threading.enumerate())
        mgr.upload_file(str(self.local), FINAL, overwrite=True)
        workers = [t for t in threading.enumerate() if t not in before]
        self._pump(20, until=device.reached.is_set)
        self.assertTrue(device.reached.is_set(), "前提: 置き換えの途中まで進まない")
        return workers

    def _assert_replaced(self, device, workers):
        self._pump(15, until=lambda: not any(t.is_alive() for t in workers))
        files = device.snapshot()
        leftovers = sorted(p for p in files if ".netbelt-part." in p)
        # 中身は大きいので、食い違いは先頭だけ見せる
        self.assertTrue(FINAL in files,
                        "最終名が消えたまま（一時名: %r）" % leftovers)
        self.assertTrue(files[FINAL] == NEW,
                        "最終名が新しい内容になっていない（先頭 %r、一時名: %r）"
                        % (files[FINAL][:30], leftovers))
        self.assertEqual([], leftovers, "一時名が機器に残った")

    def test_closing_the_tab_while_the_upload_is_being_confirmed(self):
        device = _Device(posix_rename=True, pause_at="confirm_stat")
        window = self._open_window(device)
        workers = self._upload_until_paused(window, device)

        window._on_tab_closed("sw1")

        self.assertNotIn("sw1", window.connections)
        self.assertNotIn("sw1", window.sftp_managers)
        self._assert_replaced(device, workers)

    def test_closing_the_tab_after_the_device_removed_the_final_name(self):
        device = _Device(posix_rename=False, pause_at="remove_final")
        window = self._open_window(device)
        workers = self._upload_until_paused(window, device)

        window._on_tab_closed("sw1")

        self._assert_replaced(device, workers)

    def test_closing_the_window_while_the_upload_is_being_confirmed(self):
        device = _Device(posix_rename=True, pause_at="confirm_stat")
        window = self._open_window(device)
        workers = self._upload_until_paused(window, device)

        with mock.patch("sys.excepthook", lambda *args: None):
            window.close()
        self.window_closed = True

        self._assert_replaced(device, workers)

    def test_the_device_closing_the_shell_while_the_upload_is_being_confirmed(self):
        # 機器がシェルだけを閉じる（例: exit）。SFTP のチャネルは生きている
        device = _Device(posix_rename=True, pause_at="confirm_stat",
                         close_shell_at_pause=True)
        window = self._open_window(device)
        workers = self._upload_until_paused(window, device)

        self._pump(5, until=lambda: "sw1" not in window.connections)

        self.assertNotIn("sw1", window.connections,
                         "前提: シェルが閉じたのに切断として扱われない")
        self._assert_replaced(device, workers)


class QuiesceTest(unittest.TestCase):
    """SFTPManager.quiesce は新しい操作を止め、進行中の操作を上限つきで待つ（何も書かない）"""

    def _manager(self):
        from core.sftp_manager import SFTPManager
        mgr = SFTPManager()
        mgr.sftp_client = mock.Mock()
        mgr.ssh_client = mock.Mock()
        mgr.is_connected = True
        return mgr

    def test_waits_for_the_running_operation_without_writing(self):
        mgr = self._manager()
        client = mgr.sftp_client
        mgr._sftp_lock.acquire()
        releaser = threading.Timer(0.3, mgr._sftp_lock.release)
        releaser.start()
        self.addCleanup(releaser.cancel)

        started = time.perf_counter()
        finished = mgr.quiesce()
        elapsed = time.perf_counter() - started

        self.assertTrue(finished)
        self.assertGreaterEqual(elapsed, 0.25, "進行中の操作を待たなかった")
        self.assertFalse(mgr.is_connected, "新しい操作が止まっていない")
        self.assertIs(client, mgr.sftp_client, "待つだけで閉じてはいけない")
        self.assertEqual([], client.mock_calls, "待つ間に機器へ何か書いた")
        self.assertTrue(mgr._sftp_lock.acquire(blocking=False),
                        "ロックを持ったまま戻った")
        mgr._sftp_lock.release()

    def test_gives_up_after_the_disconnect_wait(self):
        from core.sftp_manager import SFTPManager
        mgr = self._manager()
        client = mgr.sftp_client
        mgr._sftp_lock.acquire()
        self.addCleanup(mgr._sftp_lock.release)

        with mock.patch.object(SFTPManager, "_DISCONNECT_WAIT_SECONDS", 0.2):
            started = time.perf_counter()
            finished = mgr.quiesce()
            elapsed = time.perf_counter() - started

        self.assertFalse(finished)
        self.assertLess(elapsed, 1.5, "上限を過ぎても待ち続けた")
        self.assertFalse(mgr.is_connected)
        self.assertEqual([], client.mock_calls)


if __name__ == "__main__":
    unittest.main()
