"""同じグループに同名の機器が 2 台あるとき、編集が狙った 1 台に当たることを検証する。

実測（16101ef）: kyoten に rtr1@192.0.2.11 と rtr1@192.0.2.12 がある config.json
を読み、画面（MainWindow._on_device_edit）で 2 台目のパスワードを入れ直すと、
『機器 'rtr1' を更新しました』と出て update_device() は True を返すのに、
書き換わるのは 1 台目だった。update_device() は重複検査を抜けたあと
next((i for i, d in enumerate(source["devices"]) if d.get("name") == old_name))
で先頭しか掴まないためで、結果は [rtr1/192.0.2.12(新パスワード),
rtr1/192.0.2.12(暗号文)] ＝ 192.0.2.11 の機器が黙って消える。2 台目は復号
できないままなので、案内どおりパスワードを入れ直すこともできない。

直し方（利用者の決定・2026-09-20）: 接続先で見分ける。編集前の機器の接続先
（ホスト・ポート・プロトコル、シリアルならポート）を update_device() へ渡し、
同名が複数あるときは接続先の一致する 1 件を選ぶ。接続先でも 1 件に絞れない
（完全に同じ機器が 2 つある）ときは、これまでどおり先頭を選ぶ。
MainWindow._on_device_edit は編集前の device_data を持っているので、そこから
接続先を作って渡す。
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


def _device(name, host, password="", **extra):
    device = {"name": name, "host": host, "port": 22, "protocol": "ssh",
              "username": "admin", "password": password, "ssh_key": "",
              "macros": []}
    device.update(extra)
    return device


def _config():
    return {
        "config_version": "1.0",
        "groups": [
            {"name": "Default", "auto_commands": [], "devices": []},
            {"name": "kyoten", "auto_commands": [], "devices": [
                _device("rtr1", "192.0.2.11", CIPHER_A),
                _device("rtr1", "192.0.2.12", CIPHER_B),
            ]},
        ],
        "global_macros": [],
        "settings": {},
    }


class SameGroupDuplicateDeviceEditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-samedup-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        from core.config_manager import ConfigManager
        path = self.dir / "config.json"
        path.write_text(json.dumps(_config()), encoding="utf-8")
        self.cm = ConfigManager(config_path=str(path))

    def _devices(self, group_name="kyoten"):
        return [(d["name"], d["host"], d["password"])
                for d in self.cm.get_group(group_name)["devices"]]

    def _second(self):
        return dict(self.cm.get_group("kyoten")["devices"][1])

    # --- ConfigManager ---

    def test_the_endpoint_picks_the_second_entry(self):
        """接続先を渡せば、2 台目だけが書き換わること。"""
        from core.config_manager import device_endpoint
        original = self._second()

        ok = self.cm.update_device("kyoten", "rtr1", "kyoten",
                                   dict(original, password="cisco123"),
                                   old_endpoint=device_endpoint(original))

        self.assertTrue(ok)
        self.assertEqual(self._devices(),
                         [("rtr1", "192.0.2.11", CIPHER_A),
                          ("rtr1", "192.0.2.12", "cisco123")],
                         "1 台目が巻き添えで書き換わっている")

    def test_the_endpoint_picks_the_first_entry(self):
        """1 台目を指したときは 1 台目だけが書き換わること。"""
        from core.config_manager import device_endpoint
        original = dict(self.cm.get_group("kyoten")["devices"][0])

        ok = self.cm.update_device("kyoten", "rtr1", "kyoten",
                                   dict(original, password="cisco123"),
                                   old_endpoint=device_endpoint(original))

        self.assertTrue(ok)
        self.assertEqual(self._devices(),
                         [("rtr1", "192.0.2.11", "cisco123"),
                          ("rtr1", "192.0.2.12", CIPHER_B)])

    def test_an_unknown_endpoint_falls_back_to_the_first_entry(self):
        """どれとも一致しない接続先なら、これまでどおり先頭を選ぶこと。"""
        original = self._second()

        ok = self.cm.update_device("kyoten", "rtr1", "kyoten",
                                   dict(original, password="cisco123"),
                                   old_endpoint=("nobody",))

        self.assertTrue(ok)
        self.assertEqual(self._devices()[0][2], "cisco123")

    def test_without_an_endpoint_nothing_changes_for_a_single_device(self):
        """接続先を渡さない既存の呼び方も、これまでどおり通ること。"""
        self.assertTrue(self.cm.add_group("tanitsu"))
        self.assertTrue(self.cm.add_device("tanitsu", _device("sw9", "192.0.2.20")))

        ok = self.cm.update_device("tanitsu", "sw9", "tanitsu",
                                   _device("sw9", "192.0.2.21"))

        self.assertTrue(ok)
        self.assertEqual(self._devices("tanitsu"), [("sw9", "192.0.2.21", "")])

    def test_two_identical_devices_still_take_the_first(self):
        """接続先まで同じ 2 台なら、絞れないので先頭を選ぶこと。"""
        from core.config_manager import device_endpoint
        path = Path(self.cm.config_path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["groups"][1]["devices"] = [_device("rtr1", "192.0.2.11", CIPHER_A),
                                       _device("rtr1", "192.0.2.11", CIPHER_B)]
        path.write_text(json.dumps(raw), encoding="utf-8")
        from core.config_manager import ConfigManager
        self.cm = ConfigManager(config_path=str(path))
        original = self._second()

        ok = self.cm.update_device("kyoten", "rtr1", "kyoten",
                                   dict(original, password="cisco123"),
                                   old_endpoint=device_endpoint(original))

        self.assertTrue(ok)
        self.assertEqual(self._devices()[0][2], "cisco123")
        self.assertEqual(len(self._devices()), 2, "台数が変わっている")

    def test_the_endpoint_matches_the_main_window_reading(self):
        """接続先の作り方が、MainWindow._endpoint_of と同じであること。"""
        from core.config_manager import device_endpoint
        from ui.main_window import MainWindow
        for device in (_device("a", "192.0.2.1"),
                       _device("b", "192.0.2.2", protocol="telnet", port=2323),
                       _device("c", "COM3", protocol="serial", port="COM3"),
                       _device("d", "COM4", protocol="console"),
                       dict(_device("e", "COM5"), source="autodetect")):
            self.assertEqual(device_endpoint(device),
                             MainWindow._endpoint_of(device),
                             "接続先の読み方がずれている: %s" % device["name"])

    # --- 画面から ---

    def _window(self):
        from ui.main_window import MainWindow
        with mock.patch("ui.main_window.ConfigManager", return_value=self.cm), \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
                mock.patch("ui.main_window.QMessageBox.warning"):
            window = MainWindow()
        self.addCleanup(window.close)
        return window

    def test_the_device_editor_updates_the_entry_it_was_opened_from(self):
        """画面の編集で 2 台目を直したとき、1 台目が消えないこと。"""
        from PyQt6.QtWidgets import QDialog
        window = self._window()
        original = self._second()
        dialog = mock.MagicMock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_device_data.return_value = dict(original, password="cisco123")
        dialog.get_selected_group.return_value = "kyoten"
        dialog.group_combo.findText.return_value = 0

        with mock.patch("ui.main_window.DeviceDialog", return_value=dialog), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window._on_device_edit("kyoten", original)

        warn.assert_not_called()
        self.assertEqual(self._devices(),
                         [("rtr1", "192.0.2.11", CIPHER_A),
                          ("rtr1", "192.0.2.12", "cisco123")],
                         "192.0.2.11 の機器が黙って消えている")

    def test_the_second_entry_can_be_used_after_the_password_is_re_entered(self):
        """入れ直したあとは、その機器が『復号できない』扱いでなくなること。"""
        from PyQt6.QtWidgets import QDialog
        window = self._window()
        original = self._second()
        dialog = mock.MagicMock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_device_data.return_value = dict(original, password="cisco123")
        dialog.get_selected_group.return_value = "kyoten"
        dialog.group_combo.findText.return_value = 0

        with mock.patch("ui.main_window.DeviceDialog", return_value=dialog), \
                mock.patch("ui.main_window.QMessageBox.warning"):
            window._on_device_edit("kyoten", original)

        self.assertFalse(self.cm.has_undecryptable_password("rtr1", "cisco123"))


if __name__ == "__main__":
    unittest.main()
