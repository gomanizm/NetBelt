"""機器を編集して OK しただけで、保存済みのデータが消えないことを検証する。

計測で確認した経路:

  macros を 1 件、baudrate を持つ console 機器を「編集」で開き、
  何も変えずに OK → get_device_data() が返す辞書は macros が [] になり、
  baudrate キーは無い。_on_device_edit はこの辞書で丸ごと差し替えるので、
  config.json からも消える。_connect_serial は既定の 9600 で繋ぐ。

ダイアログが扱わない項目（機器別マクロ、ボーレート、将来足すキー）は、
読み込んだ辞書を土台にして、編集できる項目だけを上書きするべき。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


def console_device():
    return {
        "name": "ConsoleSW",
        "host": "COM3",
        "port": 0,
        "protocol": "console",
        "username": "",
        "password": "",
        "ssh_key": "",
        "macros": [{"name": "show ver", "commands": ["show version"]}],
        "baudrate": 115200,
    }


class DeviceDialogKeepsExtraKeysTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _edit(self, device):
        from ui.dialogs.device_dialog import DeviceDialog
        return DeviceDialog(groups=["Default"], device_data=device)

    def test_macros_survive_an_edit(self):
        """表示していた機器別マクロが、OK を押しただけで消えないこと。"""
        original = console_device()
        data = self._edit(original).get_device_data()
        self.assertEqual(data["macros"], original["macros"],
                         "編集で開いて OK しただけで macros が消える")

    def test_unknown_keys_survive_an_edit(self):
        """ダイアログが扱わないキー（baudrate）を落とさないこと。"""
        original = console_device()
        data = self._edit(original).get_device_data()
        self.assertEqual(data.get("baudrate"), 115200,
                         "編集で開いて OK しただけで baudrate が消える")

    def test_edited_fields_still_override(self):
        """土台を残しても、編集した項目はちゃんと上書きされること。"""
        dialog = self._edit(console_device())
        dialog.host_edit.setText("COM7")
        data = dialog.get_device_data()
        self.assertEqual(data["host"], "COM7")
        self.assertEqual(data["protocol"], "console")

    def test_the_loaded_dict_itself_is_not_mutated(self):
        """呼び出し側が渡した辞書を書き換えないこと。

        _on_device_edit は古い辞書の name を「改名前の名前」として使う。
        """
        original = console_device()
        dialog = self._edit(original)
        dialog.name_edit.setText("Renamed")
        dialog.get_device_data()
        self.assertEqual(original["name"], "ConsoleSW")

    def test_a_new_device_still_has_an_empty_macro_list(self):
        """新規追加では、これまでどおり macros が空リストであること。"""
        from ui.dialogs.device_dialog import DeviceDialog
        dialog = DeviceDialog(groups=["Default"])
        dialog.name_edit.setText("new")
        dialog.host_edit.setText("192.0.2.10")
        self.assertEqual(dialog.get_device_data()["macros"], [])


if __name__ == "__main__":
    unittest.main()
