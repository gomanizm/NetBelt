"""SFTP の後始末で GUI スレッドが転送の終わりを待つのは、1 回の後始末につき合計で上限（_DISCONNECT_WAIT_SECONDS）までであることを検証する。

何が起きていたか（b2858c4、localhost の paramiko 機器・本物の MainWindow・
本物の SFTPManager.download_file で実測。手元の書き込みを止めて、応答しない
共有フォルダへのダウンロードを代役にした）。1.3.2 の後始末は、quiesce()
（GUI スレッドで最大 3 秒）→ SSH を閉じる → _drop_sftp_manager →
disconnect()（もう一度最大 3 秒）の順だった。quiesce が上限で諦めたことを
誰も覚えていないので、転送スレッドが手元の I/O で止まっていて SSH を閉じても
ロックを離さないと、同じ上限を何度も待った:
  - タブの × と「切断」ボタン: GUI が 9.00 秒止まった（quiesce 3.0 → SSH の
    disconnect() が disconnected を同期で出し、_on_connection_closed が
    もう一度 quiesce 3.0 → disconnect 3.0）。1.3.1 は 3.01 秒
  - 機器がシェルを閉じる・機器が切る・送信以外のエラー: 6 秒（1.3.1 は 3 秒）
  - アプリの終了（closeEvent）: 1 台で 6.01 秒。台数ぶん積み重なり、3 台で
    18.03 秒（1.3.1 も 1 台 3 秒ずつで 9.00 秒）
Windows は 5 秒応答しない窓を「応答なし」にする。

どう直したか。SFTPManager に後始末の期限を持たせる。最初の quiesce が期限
（いま + _DISCONNECT_WAIT_SECONDS、または渡された期限）を決め、そのあとの
quiesce / disconnect は期限までの残りだけ待つ（諦めたあとは待たない）。
closeEvent は全台に同じ期限を渡す。進行中の操作はどの機器も同じ時刻から
数えて上限まで待ってもらえるので、待ちを台数ぶん積み重ねなくても、上限内に
終わる置き換えは断ち切られない。SSH を SFTP より先に閉じる順序はそのまま。

転送スレッドが手元の I/O で止まっている状態は、ロックを掴んだまま戻らない
スレッドで作る（GUI スレッドから見れば同じ）。GUI スレッドがロックの取得で
待った時間を合計して比べる。
"""
import json
import os
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

sys.path.insert(0, "src")

# テストで使う待ちの上限（秒）。本物の 3 秒だと 1 件に 9 秒かかる
LIMIT = 0.8
# 合計の待ちがこれを超えたら、上限を 2 回以上待ったと見なす
TOTAL_ALLOWED = LIMIT * 1.5


class _TimedLock:
    """SFTPManager._sftp_lock の代わり。GUI スレッドが取得で待った時間を控える"""

    def __init__(self):
        self._lock = threading.Lock()
        self.gui_waits = []

    def acquire(self, blocking=True, timeout=-1):
        started = time.perf_counter()
        try:
            return self._lock.acquire(blocking, timeout)
        finally:
            if threading.current_thread() is threading.main_thread():
                self.gui_waits.append(time.perf_counter() - started)

    def release(self):
        self._lock.release()

    def locked(self):
        return self._lock.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def _hold_lock(test, lock):
    """転送スレッドが手元の I/O で止まったまま、ロックを離さない状態を作る"""
    entered = threading.Event()
    release = threading.Event()

    def hold():
        with lock:
            entered.set()
            release.wait(30)

    threading.Thread(target=hold, daemon=True).start()
    test.assertTrue(entered.wait(5), "前提: ロックを掴めない")
    test.addCleanup(release.set)
    return release


class _EmptySFTP(paramiko.SFTPServerInterface):
    """空のフォルダだけを見せる SFTP（一覧の失敗でログを汚さない）"""

    def list_folder(self, path):
        return []

    def stat(self, path):
        attrs = paramiko.SFTPAttributes()
        attrs.st_mode = stat.S_IFDIR | 0o755
        return attrs

    lstat = stat


class _Shell(paramiko.ServerInterface):
    def __init__(self, shells):
        self.shells = shells

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        self.shells.append(channel)
        return True

    def check_channel_window_change_request(self, *args):
        return True


class _SSHDevice:
    """シェルと SFTP を持つ localhost の機器（何本でも受ける）"""

    def __init__(self):
        self.shells = []
        self.transports = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.host_key = paramiko.ECDSAKey.generate()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                accepted, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(accepted,),
                             daemon=True).start()

    def _serve(self, accepted):
        t = paramiko.Transport(accepted)
        self.transports.append(t)
        t.add_server_key(self.host_key)
        t.set_subsystem_handler("sftp", paramiko.SFTPServer, _EmptySFTP)
        try:
            t.start_server(server=_Shell(self.shells))
        except Exception:
            return
        ch = t.accept(10)
        if ch is None:
            return
        ch.sendall(b"sw# ")
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
        for t in list(self.transports):
            t.close()


class SFTPManagerTeardownDeadlineTest(unittest.TestCase):
    """quiesce が決めた期限を、そのあとの quiesce / disconnect が使い切らない"""

    def _manager(self):
        from core.sftp_manager import SFTPManager
        mgr = SFTPManager()
        mgr._sftp_lock = _TimedLock()
        mgr.sftp_client = mock.Mock()
        mgr.ssh_client = mock.Mock()
        mgr.is_connected = True
        return mgr

    def setUp(self):
        from core.sftp_manager import SFTPManager
        patcher = mock.patch.object(SFTPManager, "_DISCONNECT_WAIT_SECONDS", LIMIT)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_disconnect_after_a_timed_out_quiesce_does_not_wait_again(self):
        mgr = self._manager()
        client = mgr.sftp_client
        _hold_lock(self, mgr._sftp_lock)

        self.assertFalse(mgr.quiesce(), "前提: 上限で諦めていない")
        mgr.disconnect()

        waits = mgr._sftp_lock.gui_waits
        self.assertGreaterEqual(waits[0], LIMIT * 0.75, "quiesce が進行中の操作を待たなかった")
        self.assertLessEqual(sum(waits), TOTAL_ALLOWED,
                             "quiesce が諦めたあとの disconnect がまた待った: %r" % waits)
        self.assertIsNone(mgr.sftp_client)
        # 転送が掴んだままのクライアントは閉じない（これまでどおり）
        self.assertEqual([], client.close.mock_calls)

    def test_a_second_quiesce_does_not_wait_again(self):
        mgr = self._manager()
        _hold_lock(self, mgr._sftp_lock)

        self.assertFalse(mgr.quiesce())
        self.assertFalse(mgr.quiesce())

        self.assertLessEqual(sum(mgr._sftp_lock.gui_waits), TOTAL_ALLOWED,
                             "2 回目の quiesce がまた上限まで待った: %r"
                             % mgr._sftp_lock.gui_waits)

    def test_a_shared_deadline_bounds_the_wait_for_several_managers(self):
        managers = [self._manager() for _ in range(3)]
        for mgr in managers:
            _hold_lock(self, mgr._sftp_lock)

        deadline = time.monotonic() + LIMIT
        for mgr in managers:
            self.assertFalse(mgr.quiesce(deadline))
        for mgr in managers:
            mgr.disconnect()

        total = sum(sum(m._sftp_lock.gui_waits) for m in managers)
        self.assertLessEqual(total, TOTAL_ALLOWED,
                             "台数ぶん待ちが積み重なった: %.2f 秒" % total)

    def test_a_shared_deadline_still_lets_another_manager_finish(self):
        """先の機器で上限まで待っても、後の機器の操作が上限内に終わればそれを待てている"""
        stuck, finishing = self._manager(), self._manager()
        _hold_lock(self, stuck._sftp_lock)
        release = _hold_lock(self, finishing._sftp_lock)
        threading.Timer(LIMIT / 3, release.set).start()

        deadline = time.monotonic() + LIMIT
        self.assertFalse(stuck.quiesce(deadline))
        self.assertTrue(finishing.quiesce(deadline),
                        "上限内に終わった操作を、後の機器では待てなかった")

    def test_disconnect_without_quiesce_still_waits_for_the_running_operation(self):
        """後始末以外の disconnect（SFTP の失敗で畳むなど）の待ちは変えない"""
        mgr = self._manager()
        client = mgr.sftp_client
        release = _hold_lock(self, mgr._sftp_lock)
        threading.Timer(LIMIT / 3, release.set).start()

        mgr.disconnect()

        client.close.assert_called_once_with()


class MainWindowTeardownWaitTest(unittest.TestCase):
    """本物の SSH（localhost）で、後始末の経路ごとに GUI スレッドの待ちを合計する"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.sftp_manager import SFTPManager
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-teardown-wait-"))
        self.window_closed = False
        self.device = _SSHDevice()
        self.addCleanup(self.device.close)
        patcher = mock.patch.object(SFTPManager, "_DISCONNECT_WAIT_SECONDS", LIMIT)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pump(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end and not (until and until()):
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()

    def _devices(self, names):
        return [{"name": name, "host": "127.0.0.1", "port": self.device.port,
                 "protocol": "ssh", "username": "admin", "password": "pw"}
                for name in names]

    def _open_window(self, names=("sw1",)):
        """names の機器へ繋ぎ、SFTP が開いたら各マネージャのロックを計時つきに替える"""
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        config_path = self.dir / "config.json"
        config_path.write_text(json.dumps({
            "groups": [{"name": "Default", "devices": self._devices(names)}],
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
        for data in window.config_manager.get_groups()[0]["devices"]:
            window._on_connect_requested(data)
        self._pump(20, until=lambda: all(n in window.sftp_managers for n in names))
        for name in names:
            self.assertIn(name, window.sftp_managers, "前提: SFTP が開いていない")
        # 終了処理の MIB 読み込み待ちで、閉じるのが遅れないようにしておく
        window.snmp_panel.wait_for_background_work()
        self._pump(0.3)
        locks = []
        for name in names:
            mgr = window.sftp_managers[name]
            mgr._sftp_lock = _TimedLock()
            _hold_lock(self, mgr._sftp_lock)
            locks.append(mgr._sftp_lock)
        return window, locks

    def _close_window(self, window):
        if self.window_closed:
            return
        for name in list(window.connections):
            window._dispose_connection(name)
        with mock.patch("sys.excepthook", lambda *args: None):
            window.close()

    def _assert_waited_once(self, locks):
        waits = [w for lock in locks for w in lock.gui_waits]
        total = sum(waits)
        self.assertGreaterEqual(total, LIMIT * 0.75,
                                "進行中の転送を待たずに閉じた: %r" % waits)
        self.assertLessEqual(total, TOTAL_ALLOWED,
                             "GUI スレッドが上限を 2 回以上待った（合計 %.2f 秒、"
                             "上限 %.1f 秒）: %r" % (total, LIMIT, waits))

    def test_closing_the_tab_waits_once(self):
        window, locks = self._open_window()

        window._on_tab_closed("sw1")

        self.assertNotIn("sw1", window.connections)
        self.assertNotIn("sw1", window.sftp_managers)
        self._assert_waited_once(locks)

    def test_the_disconnect_button_waits_once(self):
        window, locks = self._open_window()
        data = window.config_manager.get_groups()[0]["devices"][0]

        with mock.patch.object(window.device_tree, "get_selected_device",
                               return_value=("Default", data)):
            window._on_disconnect_button_clicked()

        self.assertNotIn("sw1", window.connections)
        self._assert_waited_once(locks)

    def test_the_device_closing_the_shell_waits_once(self):
        window, locks = self._open_window()

        self.device.shells[-1].close()
        self._pump(10, until=lambda: "sw1" not in window.sftp_managers)

        self.assertNotIn("sw1", window.sftp_managers,
                         "前提: シェルが閉じたのに後始末をしていない")
        self._assert_waited_once(locks)

    def test_a_connection_error_waits_once(self):
        window, locks = self._open_window()
        conn = window.connections["sw1"]

        # 送信エラー以外のエラー（受信スレッドから届く）
        threading.Thread(target=conn.error_occurred.emit,
                         args=("受信エラー: テスト",), daemon=True).start()
        self._pump(10, until=lambda: "sw1" not in window.sftp_managers)

        self.assertNotIn("sw1", window.sftp_managers,
                         "前提: エラーで後始末をしていない")
        self._assert_waited_once(locks)

    def test_closing_the_window_with_two_devices_waits_once(self):
        window, locks = self._open_window(("sw1", "sw2"))

        with mock.patch("sys.excepthook", lambda *args: None):
            window.close()
        self.window_closed = True

        self._assert_waited_once(locks)


if __name__ == "__main__":
    unittest.main()
