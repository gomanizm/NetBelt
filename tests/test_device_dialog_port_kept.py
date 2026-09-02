"""機器を編集で開いただけで、保存したポート番号が変わらないことを検証する。

_create_ui は protocol_combo の currentTextChanged を _on_protocol_changed
へ繋いでいる。編集時の _load_data は保存値のポートを入れたあとに
setCurrentIndex を呼ぶが、コンボの初期選択は index 0 の "ssh" なので、
保存されたプロトコルが telnet(1) / console(2) のときだけシグナルが発火し、
_on_protocol_changed がポート欄を既定値で上書きしていた。

つまり、telnet の機器を「編集」で開いてそのまま OK を押すだけで、
カスタムポートが 23 へ書き換わって保存される。_on_device_edit は
remove_device -> add_device で丸ごと入れ替えるので、そのまま config.json
へ残る。ターミナルサーバや CML のように telnet 2001〜 を使う運用では、
編集画面を一度開いた機器へ二度と繋がらなくなる。

ssh の機器はシグナルが発火しないため無傷で、この非対称さが原因の
特定を難しくしていた。

信号を止めるだけでは直らない。_on_protocol_changed はポートの既定値
だけでなく、SSH 秘密鍵欄の有効/無効と console 用のヒントも受け持って
いるので、止めると telnet/console の機器を開いたときに鍵欄が有効なまま
残る。UI の同期と既定ポートの代入を分ける必要がある。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


def device(protocol, port):
    return {
        "name": "rtr1",
        "host": "192.0.2.10" if protocol != "console" else "COM3",
        "port": port,
        "protocol": protocol,
        "username": "admin",
        "password": "",
        "ssh_key": "",
    }


class DeviceDialogKeepsThePortTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _edit(self, protocol, port):
        from ui.dialogs.device_dialog import DeviceDialog
        return DeviceDialog(groups=["拠点A"], device_data=device(protocol, port))

    def _new(self):
        from ui.dialogs.device_dialog import DeviceDialog
        return DeviceDialog(groups=["拠点A"])

    # --- 保存値が残ること ---

    def test_editing_a_telnet_device_keeps_its_port(self):
        """ターミナルサーバの telnet 2323 が 23 に戻らないこと。"""
        dialog = self._edit("telnet", 2323)
        self.assertEqual(dialog.get_device_data()["port"], 2323,
                         "編集で開いただけでポートが既定値に戻っている")

    def test_editing_a_console_device_keeps_its_port(self):
        """console の機器でもポート欄を空にしないこと。"""
        dialog = self._edit("console", 1234)
        self.assertEqual(dialog.get_device_data()["port"], 1234,
                         "編集で開いただけでポートが消えている")

    def test_editing_an_ssh_device_keeps_its_port(self):
        """ssh の機器はこれまでどおり無傷であること。"""
        dialog = self._edit("ssh", 2222)
        self.assertEqual(dialog.get_device_data()["port"], 2222)

    def test_the_protocol_itself_is_still_loaded(self):
        """ポートを守るために、プロトコルの読み込みを止めていないこと。"""
        dialog = self._edit("telnet", 2323)
        self.assertEqual(dialog.get_device_data()["protocol"], "telnet")

    # --- 読み込み時の UI 同期が失われていないこと ---

    def test_loading_a_telnet_device_disables_the_key_field(self):
        """telnet の機器を開いたら、SSH 秘密鍵欄は無効であること。"""
        dialog = self._edit("telnet", 2323)
        self.assertFalse(dialog.ssh_key_edit.isEnabled(),
                         "telnet なのに秘密鍵欄が有効なまま")
        self.assertFalse(dialog.ssh_key_btn.isEnabled())

    def test_loading_a_console_device_disables_the_key_field(self):
        dialog = self._edit("console", 1234)
        self.assertFalse(dialog.ssh_key_edit.isEnabled(),
                         "console なのに秘密鍵欄が有効なまま")

    def test_loading_an_ssh_device_enables_the_key_field(self):
        dialog = self._edit("ssh", 2222)
        self.assertTrue(dialog.ssh_key_edit.isEnabled(),
                        "ssh なのに秘密鍵欄が無効")

    # --- 利用者が自分で選び直したときは、これまでどおり既定値を入れる ---

    def test_choosing_telnet_by_hand_fills_in_the_default_port(self):
        """新規登録でプロトコルを選んだら、既定ポートが入ること。"""
        dialog = self._new()
        dialog.protocol_combo.setCurrentText("telnet")
        self.assertEqual(dialog.port_edit.text(), "23",
                         "自分で選んだのに既定ポートが入らない")

    def test_choosing_ssh_by_hand_fills_in_the_default_port(self):
        dialog = self._new()
        dialog.protocol_combo.setCurrentText("telnet")
        dialog.protocol_combo.setCurrentText("ssh")
        self.assertEqual(dialog.port_edit.text(), "22")

    def test_changing_the_protocol_while_editing_still_updates_the_port(self):
        """編集中に利用者がプロトコルを変えたら、既定値へ入れ替えること。

        読み込みのときだけ据え置き、操作には従う。
        """
        dialog = self._edit("telnet", 2323)
        dialog.protocol_combo.setCurrentText("ssh")
        self.assertEqual(dialog.port_edit.text(), "22",
                         "操作で選び直してもポートが追従しない")


if __name__ == "__main__":
    unittest.main()
