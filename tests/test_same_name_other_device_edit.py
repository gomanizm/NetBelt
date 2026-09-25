"""同名の別の機器のタブを開いていても、もう一方の接続先と名前を変えられることを検証する。

何が起きていたか（実測、441ea02）: 手編集の config に G1/R（192.0.2.11）と
G2/R（192.0.2.12）があり、G2/R へ接続してタブを開いている。この状態で G1/R の
ホストを 192.0.2.21 へ変えると保存されず、『'R' のタブを開いている間は、
接続先を変えられません。』と断られた。G1/R を R1 へ改名しても『名前を変え
られません』と断られた。読み込みの警告は「機器の編集で名前を分けてください」
と案内しているのに、その改名もできなかった（タブを閉じれば編集できる）。

原因: MainWindow._on_device_edit の「タブを開いているか」（session_open）が
機器名だけで決まり、同名の別の機器（G2/R）のセッションでも G1/R の改名と
接続先の変更を断っていた。再接続用の写し（device_info）の差し替えは、
すでに接続先で絞っている。

利用者の決定（1）: 最小の形で、セッションの接続先（device_info の写し）が
編集した項目の接続先と違うときは、そのセッションはこの機器のものではない
として通す。写しが無いとき、接続先が同じときは、今までどおり断る。
G1/R の接続先を開いている G2/R と同じ接続先へ変える操作も通す（同名・同接続先は
今でも手編集で作れる状態で、自動実行コマンドは送らない側に倒れる）。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


def _device(name, host):
    return {"name": name, "host": host, "port": 22, "protocol": "ssh",
            "username": "admin", "password": "", "ssh_key": "", "macros": []}


G1R = _device("R", "192.0.2.11")
G2R = _device("R", "192.0.2.12")
GROUPS = [
    {"name": "Default", "auto_commands": [], "devices": []},
    {"name": "G1", "auto_commands": [], "devices": [dict(G1R)]},
    {"name": "G2", "auto_commands": [], "devices": [dict(G2R)]},
]


class SameNameOtherDeviceEditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-same-name-edit-")
        for patch in (
                mock.patch("core.config_manager.app_data_dir",
                           return_value=Path(self.dir)),
                mock.patch("ui.device_tree.list_serial_ports", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def _discard(window):
        window.terminal_widget._output_timer.stop()
        window.close()

    def _window(self, session=G2R):
        """G1/R と G2/R を持ち、「R」のタブを開いたメインウィンドウ

        session はそのタブの再接続用の写し（接続した直後と同じ）。None なら写しを置かない。
        """
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        path = os.path.join(self.dir, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"config_version": "1.0", "groups": GROUPS,
                       "global_macros": [], "settings": {}}, f)
        # 同名の機器があるので、読み込みの警告がモーダルで出る
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
                mock.patch("ui.main_window.QMessageBox.warning"):
            fake.return_value = ConfigManager(config_path=path)
            window = MainWindow()
        self.addCleanup(self._discard, window)
        window.terminal_widget.create_terminal_tab("R")
        if session is not None:
            window.device_info["R"] = dict(session)
        return window

    def _edit(self, window, group, old, change):
        """group の old を編集ダイアログで change のとおり変えて OK したことにする

        Returns:
            (警告の mock, 編集後の group の [(名前, ホスト)])
        """
        from PyQt6.QtWidgets import QDialog
        new = dict(old)
        new.update(change)
        dialog = mock.Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_device_data.return_value = new
        dialog.get_selected_group.return_value = group
        dialog.group_combo.findText.return_value = -1
        with mock.patch("ui.main_window.DeviceDialog", return_value=dialog), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window._on_device_edit(group, dict(old))
        devices = window.config_manager.get_group(group)["devices"]
        return warn, [(d["name"], d["host"]) for d in devices]

    def test_the_other_devices_endpoint_can_be_changed(self):
        window = self._window()

        warn, hosts = self._edit(window, "G1", G1R, {"host": "192.0.2.21"})

        warn.assert_not_called()
        self.assertEqual(hosts, [("R", "192.0.2.21")],
                         "G2/R のタブのせいで G1/R の接続先を変えられない")
        self.assertEqual(window.device_info["R"], G2R,
                         "開いているセッション（G2/R）の再接続用の写しを変えた")
        self.assertEqual(window.config_manager.get_group("G2")["devices"], [G2R])

    def test_the_other_device_can_be_renamed(self):
        window = self._window()

        warn, hosts = self._edit(window, "G1", G1R, {"name": "R1"})

        warn.assert_not_called()
        self.assertEqual(hosts, [("R1", "192.0.2.11")],
                         "G2/R のタブのせいで G1/R の名前を分けられない")
        self.assertEqual(window.device_info["R"], G2R,
                         "開いているセッション（G2/R）の再接続用の写しを変えた")
        self.assertNotIn("R1", window.device_info)

    def test_the_open_sessions_own_device_is_still_refused(self):
        """対照: タブを開いている G2/R そのものの接続先変更・改名は、今までどおり断る"""
        for label, change in (("接続先", {"host": "192.0.2.22"}),
                              ("名前", {"name": "R2"})):
            with self.subTest(label):
                window = self._window()

                warn, hosts = self._edit(window, "G2", G2R, change)

                warn.assert_called_once()
                self.assertEqual(hosts, [("R", "192.0.2.12")])
                self.assertEqual(window.device_info["R"], G2R)

    def test_without_the_sessions_copy_the_edit_is_still_refused(self):
        """対照: タブの接続先が分からない（写しが無い）ときは、今までどおり断る"""
        window = self._window(session=None)

        warn, hosts = self._edit(window, "G1", G1R, {"host": "192.0.2.21"})

        warn.assert_called_once()
        self.assertEqual(hosts, [("R", "192.0.2.11")])


if __name__ == "__main__":
    unittest.main()
