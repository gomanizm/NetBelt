"""同じ名前の機器の 2 台目を、機器の編集で保存できることを確かめる回帰テスト。

実測（f83be17）: config.json に kyoten1/rtr1 と kyoten2/rtr1 が入っていると、
update_device() の重複検査 `owner = self.find_device_group(new_name)` が全グループ
を通して先頭の 1 件しか見ないため、2 台目の rtr1 は owner が "kyoten1" になる。
続く `not (owner == group_name and new_name == old_name)` が成り立ち、名前を変えずに
パスワードだけ直そうとしただけで False が返る。画面（MainWindow._on_device_edit）
では『機器の更新に失敗しました。設定は変更されていません。』が出る。
復号できないパスワードを持ち込んだときの案内『該当機器のパスワードは、機器の編集で
入れ直してください』が、この機器に限って実行できない状態だった。

直し方: 名前を変えないなら、その名前は編集している当の機器のものなので衝突ではない。
改名するときだけ既存の名前かどうかを見る（同じグループの中の自分自身だけでなく、
別グループの自分自身も除いたことになる）。本当に別の既存機器の名前へ変える編集は、
これまでどおり断る。
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

# DPAPI の blob は必ずこの 20 バイト（version 1 + プロバイダ GUID）で始まる
_DPAPI_HEADER = bytes.fromhex("01000000d08c9ddf0115d1118c7a00c04fc297eb")


def _foreign(seed):
    """他の PC / アカウントで作られた体の、この PC では復号できない DPAPI 値"""
    return "DPAPI:" + base64.b64encode(
        _DPAPI_HEADER + bytes([seed]) * 300).decode("ascii")


CIPHER_A = _foreign(1)
CIPHER_B = _foreign(2)


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
             "devices": [_device("rtr1", "192.0.2.11", CIPHER_A)]},
            {"name": "kyoten2", "auto_commands": [],
             "devices": [_device("rtr1", "192.0.2.12", CIPHER_B),
                         _device("sw1", "192.0.2.13")]},
        ],
        "global_macros": [],
        "settings": {},
    }


class DuplicateDeviceNameSecondEntryEditableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-dup2nd-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        from core.config_manager import ConfigManager
        path = self.dir / "config.json"
        path.write_text(json.dumps(_config()), encoding="utf-8")
        self.cm = ConfigManager(config_path=str(path))

    def _device_of(self, group_name, name="rtr1"):
        group = self.cm.get_group(group_name)
        return dict(next(d for d in group["devices"] if d["name"] == name))

    def test_the_second_entry_can_have_its_password_re_entered(self):
        """2 台目の rtr1 も、名前を変えずにパスワードを入れ直せること。"""
        device = self._device_of("kyoten2")
        self.assertEqual(device["password"], CIPHER_B,
                         "前提: 復号に失敗して暗号文が残っている")

        ok = self.cm.update_device("kyoten2", "rtr1", "kyoten2",
                                   dict(device, password="cisco123"))

        self.assertTrue(ok, "2 台目の機器を編集で保存できない")
        self.assertEqual(self._device_of("kyoten2")["password"], "cisco123")
        self.assertEqual(self._device_of("kyoten1")["password"], CIPHER_A,
                         "1 台目まで書き換わっている")
        self.assertFalse(
            self.cm.has_undecryptable_password("rtr1", "cisco123"),
            "入れ直したのに、まだ暗号文のままと見なされている")

    def test_the_first_entry_still_works(self):
        """1 台目の編集は、これまでどおり通ること。"""
        device = self._device_of("kyoten1")
        self.assertTrue(self.cm.update_device(
            "kyoten1", "rtr1", "kyoten1", dict(device, password="cisco123")))
        self.assertEqual(self._device_of("kyoten1")["password"], "cisco123")

    def test_the_second_entry_can_be_renamed_to_a_free_name(self):
        """2 台目を、使われていない名前へ改名できること。"""
        device = self._device_of("kyoten2")
        self.assertTrue(self.cm.update_device(
            "kyoten2", "rtr1", "kyoten2", dict(device, name="rtr9")))
        self.assertEqual(
            sorted(d["name"] for d in self.cm.get_group("kyoten2")["devices"]),
            ["rtr9", "sw1"])

    def test_renaming_onto_another_devices_name_is_still_refused(self):
        """別の既存機器の名前へ変える編集は、これまでどおり断ること。"""
        device = self._device_of("kyoten2")

        self.assertFalse(self.cm.update_device(
            "kyoten2", "rtr1", "kyoten2", dict(device, name="sw1")))
        self.assertEqual(self._device_of("kyoten2")["name"], "rtr1",
                         "断ったのに書き換わっている")

    def test_the_second_entry_can_move_group_without_renaming(self):
        """名前を変えずにグループを移す編集も通ること。"""
        device = self._device_of("kyoten2")
        self.assertTrue(self.cm.update_device(
            "kyoten2", "rtr1", "Default", device))
        self.assertEqual([d["name"] for d in self.cm.get_group("Default")["devices"]],
                         ["rtr1"])

    # --- 画面から（検査役の再現と同じ経路） ---

    def _window(self):
        from ui.main_window import MainWindow
        with mock.patch("ui.main_window.ConfigManager",
                        return_value=self.cm), \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
                mock.patch("ui.main_window.QMessageBox.warning"):
            window = MainWindow()
        self.addCleanup(window.close)
        return window

    def test_the_device_editor_saves_the_second_entry(self):
        """機器の編集でパスワードだけ直したとき、失敗と言われないこと。"""
        from PyQt6.QtWidgets import QDialog

        window = self._window()
        original = self._device_of("kyoten2")
        edited = dict(original, password="cisco123")

        dialog = mock.MagicMock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_device_data.return_value = edited
        dialog.get_selected_group.return_value = "kyoten2"
        dialog.group_combo.findText.return_value = 0

        with mock.patch("ui.main_window.DeviceDialog", return_value=dialog), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window._on_device_edit("kyoten2", original)

        warn.assert_not_called()
        self.assertEqual(self._device_of("kyoten2")["password"], "cisco123")


if __name__ == "__main__":
    unittest.main()
