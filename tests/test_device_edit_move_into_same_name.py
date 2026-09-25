"""機器の編集でグループを移すとき、移動先の同名を見ることを検証する。

実測（16101ef）: ドラッグ＆ドロップの move_device は「移動先グループに既に
存在します」と断るのに、機器の編集（MainWindow._on_device_edit）でグループ欄
だけ変えた場合は素通りする。kyoten1 に rtr1@192.0.2.11、kyoten2 に
rtr1@192.0.2.12 を置いて kyoten2 の rtr1 を kyoten1 へ移すと、警告は出ずに
『機器 'rtr1' を更新しました』と表示され、kyoten1 に rtr1 が 2 台並ぶ。
名前が変わらない編集は重複検査を通らない（改名のときだけ find_device_group を
見る）ため、前の周で 2 台目を編集できるようにしたときに空いた穴。

直し方: update_device() でグループが変わるときは、移動先に同名の別の機器が
居ないかを見て、居れば move_device と同じ理由で断る。同じグループの中の編集
（グループを変えない編集）はこれまでどおり通す。
"""
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


def _device(name, host, password=""):
    return {"name": name, "host": host, "port": 22, "protocol": "ssh",
            "username": "admin", "password": password, "ssh_key": "",
            "macros": []}


def _config():
    return {
        "config_version": "1.0",
        "groups": [
            {"name": "Default", "auto_commands": [], "devices": []},
            {"name": "kyoten1", "auto_commands": [],
             "devices": [_device("rtr1", "192.0.2.11")]},
            {"name": "kyoten2", "auto_commands": [],
             "devices": [_device("rtr1", "192.0.2.12")]},
        ],
        "global_macros": [],
        "settings": {},
    }


class DeviceEditMoveIntoSameNameTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-editmove-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        from core.config_manager import ConfigManager
        path = self.dir / "config.json"
        path.write_text(json.dumps(_config()), encoding="utf-8")
        self.cm = ConfigManager(config_path=str(path))

    def _hosts(self, group_name):
        return [(d["name"], d["host"])
                for d in self.cm.get_group(group_name)["devices"]]

    def _second(self):
        return dict(self.cm.get_group("kyoten2")["devices"][0])

    def test_moving_onto_a_same_named_device_is_refused(self):
        """移動先に同名が居るなら、グループを変える編集を断ること。"""
        from core.config_manager import device_endpoint
        original = self._second()

        ok = self.cm.update_device("kyoten2", "rtr1", "kyoten1", original,
                                   old_endpoint=device_endpoint(original))

        self.assertFalse(ok, "移動先の同名を見ていない")
        self.assertEqual(self._hosts("kyoten1"), [("rtr1", "192.0.2.11")],
                         "移動先に 2 台並んでいる")
        self.assertEqual(self._hosts("kyoten2"), [("rtr1", "192.0.2.12")],
                         "断ったのに移動元から消えている")

    def test_the_refusal_is_not_saved(self):
        """断るときは設定ファイルにも触らないこと。"""
        from core.config_manager import device_endpoint
        original = self._second()

        with mock.patch.object(self.cm, "save_config") as save:
            self.assertFalse(self.cm.update_device(
                "kyoten2", "rtr1", "kyoten1", original,
                old_endpoint=device_endpoint(original)))

        self.assertFalse(save.called, "断ったのに保存している")

    def test_moving_into_a_free_group_still_works(self):
        """移動先に同名が居なければ、これまでどおり移せること。"""
        from core.config_manager import device_endpoint
        original = self._second()

        ok = self.cm.update_device("kyoten2", "rtr1", "Default", original,
                                   old_endpoint=device_endpoint(original))

        self.assertTrue(ok)
        self.assertEqual(self._hosts("Default"), [("rtr1", "192.0.2.12")])
        self.assertEqual(self._hosts("kyoten2"), [])

    def test_editing_in_place_is_unaffected(self):
        """グループを変えない編集は、同名が他グループに居ても通ること。"""
        from core.config_manager import device_endpoint
        original = self._second()

        ok = self.cm.update_device("kyoten2", "rtr1", "kyoten2",
                                   dict(original, host="192.0.2.99"),
                                   old_endpoint=device_endpoint(original))

        self.assertTrue(ok)
        self.assertEqual(self._hosts("kyoten2"), [("rtr1", "192.0.2.99")])

    def test_drag_and_drop_still_refuses_the_same_move(self):
        """前提の確認: 同じ移動を move_device は元から断っていること。"""
        self.assertFalse(self.cm.move_device("kyoten2", "kyoten1", "rtr1"))
        self.assertEqual(self._hosts("kyoten1"), [("rtr1", "192.0.2.11")])

    def test_the_device_editor_refuses_the_move(self):
        """画面の編集でも、移動先に同名が居るなら通らないこと。"""
        from PyQt6.QtWidgets import QDialog
        from ui.main_window import MainWindow
        original = self._second()
        with mock.patch("ui.main_window.ConfigManager", return_value=self.cm), \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
                mock.patch("ui.main_window.QMessageBox.warning"):
            window = MainWindow()
        self.addCleanup(window.close)
        dialog = mock.MagicMock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_device_data.return_value = dict(original)
        dialog.get_selected_group.return_value = "kyoten1"
        dialog.group_combo.findText.return_value = 0

        with mock.patch("ui.main_window.DeviceDialog", return_value=dialog), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window._on_device_edit("kyoten2", original)

        self.assertTrue(warn.called, "黙って移動している")
        self.assertEqual(self._hosts("kyoten1"), [("rtr1", "192.0.2.11")])
        self.assertEqual(self._hosts("kyoten2"), [("rtr1", "192.0.2.12")])


if __name__ == "__main__":
    unittest.main()
