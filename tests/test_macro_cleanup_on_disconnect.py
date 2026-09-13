"""切断したセッションのマクロが、次のセッションへ流れないことを検証する。

通常切断（_on_connection_closed）とエラー切断は接続を閉じるだけで、
MacroManager の cleanup_device を呼んでいなかった（呼ぶのは終了時と
タブ閉じだけ）。コマンドリストのタイマーと送信コールバックが残るので、
切断中もインデックスが進み、同名機器へ再接続した瞬間に残りのコマンドが
新しいセッションへ送られる（実測で再現）。キープアライブの CR も同じ。
機器名でしか識別していないため、再接続先のホストを変えていれば、
別の機器へ設定コマンドが飛ぶ。

接続を片付ける経路（_dispose_connection）で、その機器のマクロも止める。
また、接続直後 800ms 後に予約される自動コマンドは、その間に切断されて
いたら始めない。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


class MacroCleanupOnDisconnectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(tempfile.mkdtemp(prefix="netbelt-macro-")))
        ap.start()
        self.addCleanup(ap.stop)

    def _window(self):
        from ui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.close)
        return window

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def _running_macro(self, window, name="rtrA"):
        """接続中の機器に、長い間隔のコマンドリストとキープアライブを走らせる。"""
        sent = []
        window.connections[name] = mock.Mock()
        window.macro_manager.register_send_callback(name, sent.append)
        window.macro_manager.start_command_list(name, ["conf t", "no shut", "end"], 60000)
        window.macro_manager.start_keepalive(name, 60)
        self.assertTrue(window.macro_manager.is_command_list_active(name))
        self.assertTrue(window.macro_manager.is_keepalive_active(name))
        return sent

    def test_a_normal_disconnect_stops_the_macro(self):
        window = self._window()
        sent = self._running_macro(window)

        window._on_connection_closed("rtrA")

        mm = window.macro_manager
        self.assertFalse(mm.is_command_list_active("rtrA"),
                         "切断後もコマンドリストが動いている")
        self.assertFalse(mm.is_keepalive_active("rtrA"),
                         "切断後もキープアライブが動いている")
        self.assertNotIn("rtrA", mm._send_callbacks,
                         "切れた接続への送信コールバックが残っている")

    def test_an_error_disconnect_stops_the_macro(self):
        window = self._window()
        self._running_macro(window)

        window._on_connection_error("rtrA", "接続エラー: 経路がありません")

        self.assertFalse(window.macro_manager.is_command_list_active("rtrA"))
        self.assertNotIn("rtrA", window.macro_manager._send_callbacks)

    def test_nothing_leaks_into_a_reconnected_session(self):
        """再接続で送信先を付け直しても、前のマクロの残りが送られないこと。

        間隔を短くして、修正前なら残りのコマンドが実際に新しい接続へ
        届く時間だけ待つ（長い間隔だと待たずに通ってしまい、何も守らない）。
        """
        window = self._window()
        sent = []
        window.connections["rtrA"] = mock.Mock()
        window.macro_manager.register_send_callback("rtrA", sent.append)
        window.macro_manager.start_command_list("rtrA", ["conf t", "no shut", "end"], 100)
        window._on_connection_closed("rtrA")

        new_session = []
        window.connections["rtrA"] = mock.Mock()
        window.macro_manager.register_send_callback("rtrA", new_session.append)
        self._pump(0.6)

        self.assertEqual(new_session, [],
                         "前のセッションのマクロが新しい接続へ送られた: %s" % new_session)

    def test_auto_commands_scheduled_before_a_disconnect_do_not_start(self):
        """接続直後の自動コマンドは、予約中に切断されたら始めないこと。"""
        window = self._window()
        window.connections["rtrA"] = mock.Mock()
        group = {"name": "G", "auto_commands": ["show version"], "devices": [{"name": "rtrA"}]}
        new_session = []
        with mock.patch.object(window, "_find_group_of_device", return_value=group):
            window._run_auto_commands("rtrA")   # 800ms 後に開始が予約される
            window._on_connection_closed("rtrA")
            # 予約が発火する前に、同名で再接続された
            window.connections["rtrA"] = mock.Mock()
            window.macro_manager.register_send_callback("rtrA", new_session.append)
            self._pump(1.2)

        self.assertEqual(new_session, [],
                         "前の接続で予約した自動コマンドが新しい接続へ送られた: %s" % new_session)


    def test_a_disconnect_clears_the_tabs_keepalive_mark(self):
        """切断でキープアライブが止まったら、タブ側の「動作中」の印も消えること。

        印が残ると、再接続後の右クリックメニューが「キープアライブ停止」を
        出し続け、一度「停止」を押さないと入れ直せない。
        """
        window = self._window()
        terminal = window.terminal_widget.create_terminal_tab("rtrA")
        window.connections["rtrA"] = mock.Mock()
        window.macro_manager.register_send_callback("rtrA", lambda s: None)
        window._start_keepalive("rtrA", 60)
        self.assertTrue(terminal._keepalive_active)

        window._on_connection_closed("rtrA")

        self.assertFalse(window.macro_manager.is_keepalive_active("rtrA"))
        self.assertFalse(terminal._keepalive_active,
                         "切断後もタブにキープアライブ動作中の印が残っている")


if __name__ == "__main__":
    unittest.main()
