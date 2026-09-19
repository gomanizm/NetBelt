"""タブを開いている機器の接続先（ホスト・ポート・プロトコル、シリアルならポート）の変更を断ることを検証する。

何が起きていたか（実測）: 接続中の rtr1（192.0.2.10）を名前はそのままに
ホストを 192.0.2.20 へ編集すると、保存されて接続先リストは新しいホストを
示すが、タブ・接続は古いホストのまま残った。「ツール → マクロ実行」は
機器名でセッションを引くので、選んだマクロは古いホスト（192.0.2.10）へ
送られ、再接続用の写し（device_info）だけが新しいホストになっていた。

利用者の決定（2026-09-20）: 改名と同じく、タブを開いている（接続が残って
いる）機器の接続先の変更は断る。文言は改名を断るときと揃え、『'X' のタブを
開いている間は、接続先を変えられません。タブを閉じてから変更してください。
（今回の変更は保存していません）』とする。接続先以外（パスワード・ユーザー名・
鍵・マクロ設定など）の変更は、これまでどおり保存する。

実装: _on_device_edit で、改名の判定のあとに、編集前の項目と編集後の機器
データの接続先を _endpoint_of() で比べ、タブか接続が残っていて違えば断る。
あわせて、再接続用の写しを差し替えるのは、写しが編集した項目と同じ接続先の
ときだけにする。名前だけが同じ別の接続先（自動検出の COM3 と登録機器
「COM3」）の編集で写しを上書きすると、開いているセッションの接続先が
編集した機器へ変わってしまい、「ツール」の照合（D1）も Enter の再接続も
別の機器を指すため。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


def _device(name="rtrA", **extra):
    data = {"name": name, "host": "192.0.2.10", "port": 22, "protocol": "ssh",
            "username": "cisco", "password": "", "ssh_key": "", "macros": []}
    data.update(extra)
    return data


def _console(name="con1", host="COM3", **extra):
    data = {"name": name, "host": host, "port": 0, "protocol": "console",
            "username": "", "password": "", "ssh_key": "", "macros": [],
            "baudrate": 9600}
    data.update(extra)
    return data


def _autodetected(port="COM3", baudrate=9600):
    """自動検出項目と同じ形の機器データを返す。"""
    return {"name": port, "type": "serial", "protocol": "serial", "port": port,
            "baudrate": baudrate, "description": "USB Serial Port",
            "source": "autodetect"}


class EndpointChangeWithOpenTabRefusedTest(unittest.TestCase):
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
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = tempfile.mkdtemp(prefix="netbelt-endpoint-open-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
                mock.patch("ui.device_tree.list_serial_ports", return_value=[]):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            window = MainWindow()
        type(self)._windows.append(window)
        return window

    def _edit(self, window, old, new):
        """編集ダイアログで new を入力して OK したことにする。(警告, 保存) を返す。"""
        from PyQt6.QtWidgets import QDialog
        dialog = mock.Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_device_data.return_value = new
        dialog.get_selected_group.return_value = "Default"
        dialog.group_combo.findText.return_value = 0
        with mock.patch("ui.main_window.DeviceDialog", return_value=dialog), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn, \
                mock.patch.object(window, "_load_devices"), \
                mock.patch.object(window.config_manager, "update_device",
                                  return_value=True) as update_device:
            window._on_device_edit("Default", old)
        return warn, update_device

    def _open(self, window, data):
        """data の機器のタブを開き、再接続用の写しを置く（接続した直後と同じ）。"""
        window.terminal_widget.create_terminal_tab(data["name"])
        window.device_info[data["name"]] = data

    def test_changing_the_endpoint_with_an_open_tab_is_refused(self):
        """タブを開いている機器の接続先は変えられず、タブを閉じるよう伝えること。"""
        cases = [
            ("ホスト", _device(), _device(host="192.0.2.20")),
            ("ポート", _device(), _device(port=2222)),
            ("プロトコル", _device(), _device(protocol="telnet", port=23)),
            ("シリアルのポート", _console(), _console(host="COM4")),
        ]
        for label, old, new in cases:
            with self.subTest(label):
                window = self._window()
                self._open(window, old)

                warn, update_device = self._edit(window, old, new)

                self.assertFalse(update_device.called,
                                 "タブを開いたまま接続先を変えられてしまう")
                warn.assert_called_once()
                message = warn.call_args[0][2]
                self.assertIn("'%s' のタブを開いている間は、接続先を変えられません。"
                              % old["name"], message)
                self.assertIn("タブを閉じてから変更してください。", message,
                              "どうすれば変えられるかを伝えていない")
                self.assertIn("（今回の変更は保存していません）", message)
                self.assertEqual(window.device_info[old["name"]], old,
                                 "断ったのに再接続用の写しを変えた")

    def test_changing_the_endpoint_with_a_live_connection_is_refused(self):
        """接続だけが残っている場合（タブを作る前など）も断ること。"""
        window = self._window()
        old = _device()
        window.connections["rtrA"] = object()
        self.addCleanup(window.connections.pop, "rtrA", None)

        warn, update_device = self._edit(window, old, _device(host="192.0.2.20"))

        self.assertFalse(update_device.called, "接続中のまま接続先を変えられてしまう")
        warn.assert_called_once()

    def test_other_changes_with_an_open_tab_are_saved(self):
        """接続先以外の変更は、タブを開いていても保存し、再接続用の写しにも届くこと。"""
        cases = [
            ("パスワード", dict(password="new-secret")),
            ("ユーザー名", dict(username="admin")),
            ("鍵", dict(ssh_key=r"C:\keys\netbelt_test2")),
            ("ポートの型だけ", dict(port="22")),
        ]
        for label, change in cases:
            with self.subTest(label):
                window = self._window()
                old = _device()
                self._open(window, old)
                new = _device(**change)

                warn, update_device = self._edit(window, old, new)

                self.assertTrue(update_device.called, "接続先以外の変更まで断っている")
                warn.assert_not_called()
                self.assertEqual(window.device_info["rtrA"], new,
                                 "再接続用の写しに変更が届いていない")

    def test_a_device_without_a_tab_can_change_its_endpoint(self):
        """タブを開いていない機器は、これまでどおり接続先を変えられること。"""
        window = self._window()
        old = _device()

        warn, update_device = self._edit(window, old, _device(host="192.0.2.20"))

        self.assertTrue(update_device.called, "タブの無い機器まで接続先の変更を断っている")
        warn.assert_not_called()

    def test_editing_a_same_named_device_leaves_the_open_sessions_copy(self):
        """名前だけが同じ別の接続先を編集しても、開いているセッションの写しを上書きしないこと。

        自動検出の COM3 へ繋いでいる間に、登録機器「COM3」（SSH）のパスワードを
        変えた場合。変更は保存するが、再接続用の写しはシリアルのまま残す。
        """
        window = self._window()
        session = _autodetected("COM3")
        self._open(window, session)
        registered = _device("COM3")

        warn, update_device = self._edit(window, registered,
                                         _device("COM3", password="new-secret"))

        self.assertTrue(update_device.called, "接続先を変えていない編集を断っている")
        warn.assert_not_called()
        self.assertEqual(window.device_info["COM3"], session,
                         "開いているシリアルのセッションの写しが SSH 機器に置き換わった")


if __name__ == "__main__":
    unittest.main()
