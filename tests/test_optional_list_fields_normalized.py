"""任意項目（機器の macros・グループの auto_commands）の型が壊れた config.json でも、
編集画面を開けることを検証する。

何が起きていたか（実測）: 読み込みは name / host しか見ないので、手編集や
他ツールが書いた `"macros": null` / `"macros": [null]` / `"auto_commands": null`
はそのまま残り、一覧には出るのに編集ダイアログが例外で開けなかった。

    macros=None        -> TypeError("'NoneType' object is not iterable")
                          （device_dialog.py の for macro in ...get("macros", [])）
    macros=[None]      -> AttributeError("'NoneType' object has no attribute 'get'")
                          （同じ行の macro.get("name", "")）
    auto_commands=None -> TypeError("'NoneType' object is not iterable")
                          （main_window.py の list(group.get("auto_commands", []))）

読み込み後も直らないまま残っていた:

    load_error: None
    group: {'name': 'Lab', 'auto_commands': None, 'devices': []}
    devices after load: [{'name': 'R1', 'host': '192.0.2.1', ..., 'macros': None}]

install_excepthook のおかげでアプリは落ちないが、編集ダイアログが開かない
ままなので、その機器は編集で直せずパスワードも入れ直せない。

どう直したか: 読み込み時の隔離（_quarantine_invalid_devices）で、macros と
auto_commands をリストへそろえる。null は「無し」と同じ意味なので黙って
そろえ（パスワードの null と同じ扱い）、リストでない値やリストの中の
読めない要素は外して、name / host の隔離と同じように警告で知らせる。
読み手側（DeviceDialog と MainWindow._on_edit_group）にも、リストでない値を
そのまま回さない保険を入れる。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


def _device(name, **extra):
    data = {"name": name, "host": "192.0.2.1", "port": 22, "protocol": "ssh",
            "username": "u", "password": "", "ssh_key": ""}
    data.update(extra)
    return data


class OptionalListFieldsNormalizedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-optional-lists-")
        patch = mock.patch("core.config_manager.app_data_dir",
                           return_value=Path(self.dir))
        patch.start()
        self.addCleanup(patch.stop)

    def _load(self, groups):
        """その中身の config.json を読み込んだ ConfigManager を返す"""
        from core.config_manager import ConfigManager
        path = os.path.join(self.dir, "config-%d.json" % len(os.listdir(self.dir)))
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"config_version": "1.0", "groups": groups,
                       "global_macros": [], "settings": {}}, f)
        manager = ConfigManager(config_path=path)
        self.assertIsNone(manager.load_error, "前提: JSON としては読めている")
        return manager

    def test_broken_macros_are_normalized_on_load(self):
        """機器の macros がリストでなければ空のリストにし、読めない要素は外すこと。"""
        macro = {"name": "show", "commands": ["show ver"]}
        manager = self._load([{"name": "Lab", "auto_commands": [], "devices": [
            _device("R1", macros=None),
            _device("R2", macros=[None, macro]),
            _device("R3", macros="show ver"),
            _device("R4", macros=[macro]),
        ]}])
        got = {d["name"]: d.get("macros")
               for d in manager.get_group("Lab")["devices"]}
        self.assertEqual(got, {"R1": [], "R2": [macro], "R3": [], "R4": [macro]},
                         "読み込みで macros がそろっていない: %r" % (got,))

    def test_broken_auto_commands_are_normalized_on_load(self):
        """auto_commands は、読めない要素が 1 つでもあれば一覧ごと無効にすること。

        このテストは当初 B を ["terminal length 0"] と期待していた（読める
        要素だけ残す）。同じ周に入った conn-04 の修正が、自動実行コマンドは
        接続しただけで実機へ流れるため「読めない形なら一覧ごと送らない」と
        決めている（tests/test_config_auto_commands_type.py）。読める分だけ
        送ると、利用者が書いた並びの一部だけが機器へ流れることになるので、
        送らない側へそろえた。null は「無し」と同じ意味なので、これまでどおり
        黙って空のリストにそろえる（機器の macros は機器へ送らないので、
        読める分を残す扱いのまま）。
        """
        manager = self._load([
            {"name": "A", "auto_commands": None, "devices": []},
            {"name": "B", "auto_commands": ["terminal length 0", None, 7],
             "devices": []},
            {"name": "C", "auto_commands": "terminal length 0", "devices": []},
            {"name": "D", "auto_commands": ["terminal length 0"], "devices": []},
        ])
        got = {g["name"]: g.get("auto_commands") for g in manager.get_groups()
               if g["name"] in ("A", "B", "C", "D")}
        self.assertEqual(got, {"A": [], "B": [], "C": [],
                               "D": ["terminal length 0"]},
                         "読み込みで auto_commands がそろっていない: %r" % (got,))

    def test_null_is_normalized_without_a_warning(self):
        """null は「無し」と同じ意味なので、黙ってそろえること（パスワードの null と同じ）。"""
        manager = self._load([{"name": "Lab", "auto_commands": None,
                               "devices": [_device("R1", macros=None)]}])
        self.assertIsNone(manager.load_warning,
                          "null をそろえただけで警告が出た: %r" % manager.load_warning)
        self.assertIsNone(manager.backup_path,
                          "null をそろえただけでバックアップを取った")

    def test_dropping_unreadable_entries_is_reported(self):
        """外した分は、name / host の隔離と同じように知らせること。"""
        manager = self._load([{"name": "Lab", "auto_commands": [None],
                               "devices": [_device("R1", macros=["x"])]}])
        warning = manager.load_warning or ""
        self.assertIn("R1", warning,
                      "マクロを外した機器を知らせていない: %r" % warning)
        self.assertIn("Lab", warning,
                      "自動実行コマンドを外したグループを知らせていない: %r" % warning)

    def test_device_dialog_opens_with_broken_macros(self):
        """読み手側の保険: macros が壊れていても編集ダイアログが開くこと。"""
        from ui.dialogs.device_dialog import DeviceDialog
        for macros in (None, [None], "show ver", [{"name": "ok"}, None]):
            with self.subTest(macros=macros):
                dialog = DeviceDialog(None, groups=["Default"],
                                      device_data=_device("R1", macros=macros))
                self.assertEqual(dialog.name_edit.text(), "R1")
                dialog.deleteLater()

    def test_edit_group_opens_with_broken_auto_commands(self):
        """読み手側の保険: auto_commands が壊れていても編集ダイアログが開くこと。"""
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        from PyQt6.QtWidgets import QDialog
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(self.dir, "main.json"))
            window = MainWindow()
        self.addCleanup(window.close)
        window.config_manager.add_group("Lab")
        # 読み込みを経ずに壊れた値が入ってきた場合（他の経路からの持ち込み）
        window.config_manager.get_group("Lab")["auto_commands"] = None
        with mock.patch.object(QDialog, "exec",
                               return_value=QDialog.DialogCode.Rejected):
            window._on_edit_group("Lab")


if __name__ == "__main__":
    unittest.main()
