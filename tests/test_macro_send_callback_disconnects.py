"""送信コールバックの中で切断されても、マクロ側が例外を出さないことを検証する。

key_pressed -> 接続の send_command は同一スレッドの直結なので、送信に失敗
すると同期で error_occurred -> _on_connection_error -> _on_connection_closed
-> _dispose_connection -> macro_manager.cleanup_device まで一気に走る。
戻ってきた _execute_next_command は、消えたばかりの _command_delays を
読んで KeyError を投げていた（main.py の install_excepthook が拾うので
abort はせず「予期しないエラーが発生しました」ダイアログになる）。
さらに _command_indices[device_name] だけが再作成されて残る。

コールバックから戻った時点でその機器の実行が畳まれていたら、
インデックス・遅延・タイマーには触らずに戻る。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class MacroSendCallbackDisconnectsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _manager(self):
        from core.macro_manager import MacroManager
        manager = MacroManager()
        self.addCleanup(manager.cleanup_device, "dev")
        return manager

    def test_a_disconnect_inside_the_send_callback_does_not_raise(self):
        """送信の途中でセッションが落ちても KeyError にならないこと。"""
        manager = self._manager()
        sent = []

        def send(text):
            sent.append(text)
            # 送信失敗 -> エラー通知 -> 接続の片付け、が同期で走るのを模す
            manager.cleanup_device("dev")

        manager.register_send_callback("dev", send)
        manager.start_command_list("dev", ["cmd1", "cmd2"], 50)

        self.assertEqual(sent, ["cmd1\r"], "前提: 1本目は送られる")

    def test_the_device_leaves_no_state_behind_after_such_a_disconnect(self):
        """畳まれたあとに、その機器の実行状態が作り直されて残らないこと。"""
        manager = self._manager()

        def send(text):
            manager.cleanup_device("dev")

        manager.register_send_callback("dev", send)
        manager.start_command_list("dev", ["cmd1", "cmd2"], 50)

        self.assertNotIn("dev", manager._command_indices,
                         "片付けた後にインデックスが作り直されている: %r"
                         % manager._command_indices)
        self.assertNotIn("dev", manager._command_lists)
        self.assertFalse(manager.is_command_list_active("dev"))


if __name__ == "__main__":
    unittest.main()
