"""古い接続スレッドの通知が、同名の新しい接続を壊さないことを検証する。

_connect_ssh などは connected / disconnected / error_occurred を
「機器名だけ」を束縛したラムダで受けていた。接続中にタブを閉じて
同名で繋ぎ直すと、旧スレッドは TCP タイムアウト（最大 20〜30 秒）の
あとで error_occurred を出し、ハンドラは機器名で辞書を引いて **新しい**
接続を dispose してしまう。旧スレッドが遅れて成功した場合は、旧
セッションの出力が新しいタブへ混ざり、SFTP マネージャも旧接続の
client 上に作り直される。

計測: 旧スレッドが connect で止まっている間にタブを閉じて再接続し、
旧スレッドのエラーを流すと、新しい接続は is_connected False /
client None になり、新しいタブに「接続エラー: timed out」が出た。
sftp_managers には閉じた client を抱えたマネージャが残った。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class StaleConnectionNotificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-stale-")
        with mock.patch("ui.main_window.ConfigManager") as fake,              mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            w = MainWindow()
        type(self)._windows.append(w)
        return w

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    # --- ハンドラ単体: 束縛された接続が現在のものでなければ無視する ---

    def test_an_error_from_a_replaced_connection_leaves_the_new_one_alone(self):
        w = self._window()
        old, new = mock.Mock(), mock.Mock()
        w.connections["R"] = new
        w.terminal_widget.create_terminal_tab("R")

        with mock.patch.object(w.terminal_widget, "show_notice") as notice:
            w._on_connection_error("R", "接続エラー: timed out", old)

        new.dispose.assert_not_called()
        self.assertIs(w.connections.get("R"), new, "新しい接続が辞書から消えた")
        notice.assert_not_called()
        old.dispose.assert_called_once()

    def test_a_close_from_a_replaced_connection_leaves_the_new_one_alone(self):
        w = self._window()
        old, new = mock.Mock(), mock.Mock()
        w.connections["R"] = new
        w.terminal_widget.create_terminal_tab("R")

        with mock.patch.object(w.terminal_widget, "show_notice") as notice:
            w._on_connection_closed("R", old)

        new.dispose.assert_not_called()
        self.assertIs(w.connections.get("R"), new)
        notice.assert_not_called()

    def test_a_late_success_from_a_replaced_connection_is_discarded(self):
        """旧スレッドが遅れて成功しても、新しいセッションに割り込まないこと。"""
        from core.ssh_connection import SSHConnection
        w = self._window()
        terminal = w.terminal_widget.create_terminal_tab("R")
        old = SSHConnection("192.0.2.1", 22, "admin", parent=w)
        old.client = mock.Mock()
        old.is_connected = True
        new = mock.Mock()
        w.connections["R"] = new

        w._on_connection_success("R", terminal, old)
        self._pump(0.3)

        self.assertIs(w.connections.get("R"), new)
        self.assertNotIn("R", w.sftp_managers,
                         "旧接続の client で SFTP マネージャが作られた")
        self.assertIsNone(old.client, "遅れて成功した旧接続が閉じられていない")

    def test_notifications_without_a_bound_connection_still_work(self):
        """接続を束縛しない呼び出し（既存の経路）は、これまでどおり効くこと。"""
        w = self._window()
        conn = mock.Mock()
        w.connections["R"] = conn
        w._on_connection_error("R", "読み取りエラー: 切断されました")
        conn.dispose.assert_called_once()
        self.assertNotIn("R", w.connections)

    def test_an_error_on_the_current_connection_still_disposes_it(self):
        w = self._window()
        conn = mock.Mock()
        w.connections["R"] = conn
        w._on_connection_error("R", "接続エラー: timed out", conn)
        conn.dispose.assert_called_once()
        self.assertNotIn("R", w.connections)

    def test_an_error_drops_the_sftp_manager_too(self):
        """エラーで接続を捨てるとき、閉じた client を抱えたマネージャを残さないこと。"""
        w = self._window()
        conn = mock.Mock()
        w.connections["R"] = conn
        manager = mock.Mock()
        w.sftp_managers["R"] = manager
        w.sftp_panel.set_sftp_manager(manager, "R")

        w._on_connection_error("R", "接続エラー: timed out", conn)

        self.assertNotIn("R", w.sftp_managers)
        manager.disconnect.assert_called_once()
        self.assertFalse(w.sftp_panel.current_device)

    # --- 実際の配線: タブを閉じて同名で再接続したあと、旧スレッドの通知 ---

    def test_a_late_error_from_the_old_thread_does_not_kill_the_reconnect(self):
        from core.ssh_connection import SSHConnection
        w = self._window()
        gates = []

        def blocked_connect(conn):
            gate = threading.Event()
            gates.append(gate)
            gate.wait(5)
            conn.error_occurred.emit("接続エラー: timed out")
            return False

        device = {"name": "R", "host": "192.0.2.1", "port": 22,
                  "protocol": "ssh", "username": "admin", "password": "pw"}
        with mock.patch.object(SSHConnection, "connect", blocked_connect):
            w._connect_ssh(device)
            self._pump(0.1)
            old = w.connections["R"]
            self.assertEqual(len(gates), 1, "前提: 旧スレッドが connect で止まっている")

            # 接続中にタブを閉じ、同名で繋ぎ直す
            w._on_tab_closed("R")
            self.assertNotIn("R", w.connections)
            w._connect_ssh(device)
            self._pump(0.1)
            new = w.connections["R"]
            self.assertIsNot(new, old)
            new.is_connected = True      # 新しい方は繋がったことにする
            new.client = mock.Mock()

            # 旧スレッドのタイムアウトが遅れて届く
            gates[0].set()
            self._pump(0.3)

            self.assertIs(w.connections.get("R"), new,
                          "旧スレッドの失敗通知が新しい接続を捨てた")
            self.assertTrue(new.is_connected)
            self.assertIsNotNone(new.client)

            # 後片付け
            gates[1].set()
            self._pump(0.2)
            w._on_tab_closed("R")

    def test_a_late_output_from_the_old_thread_does_not_mix_into_the_new_tab(self):
        """旧接続の出力が新しいタブへ流れないこと。

        ハンドラを直接叩く単体テストは、接続を引数に取る署名になったことしか
        確かめられない（同一性検査を外しても素通りする）。ここは _connect_ssh
        が張った配線をそのまま使い、出力経路の挙動そのものを固定する。
        """
        from core.ssh_connection import SSHConnection
        w = self._window()
        gates = []

        def blocked_connect(conn):
            gate = threading.Event()
            gates.append(gate)
            gate.wait(5)
            conn.output_received.emit("旧セッションの出力\r\n")
            return True

        device = {"name": "R", "host": "192.0.2.1", "port": 22,
                  "protocol": "ssh", "username": "admin", "password": "pw"}
        appended = []
        with mock.patch.object(SSHConnection, "connect", blocked_connect), \
                mock.patch.object(w.terminal_widget, "append_output",
                                  side_effect=lambda d, t: appended.append((d, t))):
            w._connect_ssh(device)
            self._pump(0.1)
            old = w.connections["R"]

            # 接続中にタブを閉じ、同名で繋ぎ直す
            w._on_tab_closed("R")
            w._connect_ssh(device)
            self._pump(0.1)
            new = w.connections["R"]
            self.assertIsNot(new, old)

            # 旧スレッドの出力が遅れて届く
            gates[0].set()
            self._pump(0.3)
            # 切断バナー（show_notice も append_output を通る）は対象外
            self.assertNotIn(("R", "旧セッションの出力\r\n"), appended,
                             "旧接続の出力が新しいタブへ流れた")

            # 新しい接続の出力はこれまでどおり流れる
            new.output_received.emit("新セッションの出力\r\n")
            self._pump(0.2)
            self.assertIn(("R", "新セッションの出力\r\n"), appended,
                          "現在の接続の出力まで捨てている")

            # 後片付け
            gates[1].set()
            self._pump(0.2)
            w._on_tab_closed("R")

    def test_a_failed_connect_still_reaches_the_tab(self):
        """接続に失敗したら、利用者に見える形で伝わること。

        接続クラスは失敗のとき先に error_occurred を出すので、
        _on_connection_error が connections から外したあとに connect_thread
        の「接続失敗」が届く。置き換えられた接続からの通知ではないのに、
        同一性検査でこれが捨てられていた（実測: append_output 呼び出し 0 件）。
        """
        from core.ssh_connection import SSHConnection
        w = self._window()
        appended = []

        def failing_connect(conn):
            conn.error_occurred.emit("接続エラー: timed out")
            return False

        device = {"name": "R", "host": "192.0.2.1", "port": 22,
                  "protocol": "ssh", "username": "admin", "password": "pw"}
        with mock.patch.object(SSHConnection, "connect", failing_connect), \
                mock.patch.object(w.terminal_widget, "append_output",
                                  side_effect=lambda d, t: appended.append((d, t))), \
                mock.patch.object(w.terminal_widget, "show_notice") as notice:
            w._connect_ssh(device)
            self._pump(0.5)

        self.assertIn(("R", "\r\n接続失敗\r\n"), appended,
                      "接続に失敗したのに、タブには何も出ない")
        self.assertTrue(notice.called, "エラーの内容も伝わっていない")


if __name__ == "__main__":
    unittest.main()
