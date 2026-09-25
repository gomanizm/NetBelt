"""config.json の settings.syslog.max_messages が異常値でも受信で落ちないこと。

実測（基準 16101ef）: config.json に手で max_messages を書いて SyslogPanel を
作り、UDP で本物の Syslog を 1 件送ると、SyslogPanel.add_message
（SyslogReceiver.message_received に繋がるスロット。src/ui/main_window.py:390）
の中で次の例外が出た。
  - null  -> TypeError: '>=' not supported between instances of 'int' and 'NoneType'
  - "abc" -> TypeError: '>=' not supported between instances of 'int' and 'str'
  - 0, -1 -> IndexError: pop from empty list
            （src/ui/syslog_panel.py:189 の len(self.messages) >= self.max_messages が
              常に真になり、beginRemoveRows の後の pop(0) が空リストで失敗する。
              endRemoveRows へ到達しないのでモデルの行削除通知も開いたまま残る）
例外はスロットの中を抜けるため、PyQt6 がプロセスを落とす。実測では
processEvents() の最中に終了コード -1073740791 (0xC0000409) で終了し、
Traceback も Qt のメッセージも一切出なかった（1 件目の受信で無言で落ちる）。
max_messages は GUI からもアプリからも書かれないので、到達するのは手編集の
config.json だけ。既存 tests/test_syslog_panel_null_settings.py は
settings / settings.syslog というセクション全体が dict でない場合しか見ていない。

直し方: _load_config で max_messages を int へ寄せて検算し、int() が通って
1 以上のときだけ採用する（それ以外は既定値 1000）。併せて
SyslogTableModel.add_message は、上限が 1 未満・非数でも
beginRemoveRows/pop へ進まないようにし、保持している行がある場合にだけ
削除する。auto_scroll と同じく、値が不正でも起動と受信は続く。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


def _write_config(path, syslog_section):
    config = {
        "config_version": "1.0",
        "groups": [{"name": "Default", "auto_commands": [], "devices": []}],
        "global_macros": [],
        "settings": {"syslog": syslog_section},
        "update_settings": {"check_on_startup": False},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f)


class SyslogPanelBadMaxMessagesTest(unittest.TestCase):
    _keep = []   # 配送待ちシグナルの宛先を先に解放しない（他テストと同じ理由）

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.app.processEvents()
        cls._keep.clear()

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-bad-max-")
        self.path = os.path.join(self.dir, "config.json")
        home = mock.patch("core.config_manager.app_data_dir",
                          return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)

    def _panel(self, syslog_section):
        from core.config_manager import ConfigManager
        from ui.syslog_panel import SyslogPanel
        _write_config(self.path, syslog_section)
        panel = SyslogPanel(config_manager=ConfigManager(config_path=self.path))
        self._keep.append(panel)
        return panel

    @staticmethod
    def _incoming(text="<190>Sep 20 10:00:00 rtr1: link up"):
        """受信スレッドが作るのと同じ形のメッセージ"""
        from core.syslog_receiver import SyslogMessage
        return SyslogMessage(text, "192.0.2.1", proto="UDP", port=514)

    # --- 上限値の検算 ---------------------------------------------------

    def test_bad_max_messages_falls_back_to_the_default(self):
        for value in (None, 0, -1, "abc", [], {}, 0.5, True, False):
            with self.subTest(value=value):
                panel = self._panel({"max_messages": value})
                self.assertEqual(panel.max_messages, 1000)
                self.assertEqual(panel.model.max_messages, 1000)

    def test_usable_max_messages_is_still_used(self):
        for value, expected in ((250, 250), ("250", 250), (1, 1), (2.0, 2)):
            with self.subTest(value=value):
                panel = self._panel({"max_messages": value})
                self.assertEqual(panel.max_messages, expected)
                self.assertEqual(panel.model.max_messages, expected)

    # --- 受信スロットが例外を抜けさせないこと ---------------------------

    def test_receiving_a_message_does_not_raise_for_bad_limits(self):
        for value in (None, 0, -1, "abc"):
            with self.subTest(value=value):
                panel = self._panel({"max_messages": value})
                panel.add_message(self._incoming())
                panel.add_message(self._incoming())
                panel.add_message(self._incoming())
                # 既定値へ戻っているので 3 件とも残る
                self.assertEqual(len(panel.model.messages), 3)
                self.assertEqual(panel.model.rowCount(), 3)

    def test_model_keeps_rows_when_its_limit_is_broken_directly(self):
        """モデルの上限が壊れていても行削除通知を開きっぱなしにしない"""
        from ui.syslog_panel import SyslogMessage, SyslogTableModel
        for limit in (None, 0, -1, "abc"):
            with self.subTest(limit=limit):
                model = SyslogTableModel(limit)
                self._keep.append(model)
                removed = []
                model.rowsAboutToBeRemoved.connect(
                    lambda parent, first, last, log=removed: log.append((first, last)))
                model.rowsRemoved.connect(
                    lambda parent, first, last, log=removed: log.append(("done", first)))
                for i in range(3):
                    model.add_message(SyslogMessage("t", "rtr1", "Info",
                                                    "m%d" % i, "raw", "192.0.2.1"))
                # 空のリストから削除しようとしない（受け取った 1 件は必ず残る）
                self.assertGreaterEqual(len(model.messages), 1)
                self.assertEqual(model.rowCount(), len(model.messages))
                # 開始した削除は必ず閉じている（開始があれば同数の完了がある）
                starts = [x for x in removed if x[0] != "done"]
                ends = [x for x in removed if x[0] == "done"]
                self.assertEqual(len(starts), len(ends))
                # 数として読める上限が無ければ件数で捨てない
                if limit in (None, "abc"):
                    self.assertEqual(len(model.messages), 3)


if __name__ == "__main__":
    unittest.main()
