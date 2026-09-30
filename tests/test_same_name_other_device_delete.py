"""同名の別の機器のタブを開いていても、もう一方を削除できることを検証する。

何が起きていたか（実測、441ea02）: 手編集の config に G1/R（192.0.2.11）と
G2/R（192.0.2.12）があり、G2/R へ接続してタブを開いている。この状態で
G1/R を右クリック →「削除」すると、『'R' のタブを開いている間は、削除
できません。』と断られ、消えなかった（編集の守りと同じく、名前だけで判定
していた）。さらに、削除が通ったときの後始末も再接続用の写し
（device_info['R']）を名前だけで消すので、守りだけを緩めると、開いている
G2/R のタブの写しまで消え、Enter の再接続が『再接続情報が見つかりません』に
なる。

利用者の決定（入れる）: 編集と同じ考え方で、セッションの写しの接続先が
削除する項目の接続先と違えば、そのセッションは同名の別の機器のものとして
削除を通す。写しを消すのは、写しが無いか接続先が同じときだけにする。
項目の機器データが渡されない呼び出し、写しが無いタブ、接続先が同じタブは、
今までどおり断る。
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


class SameNameOtherDeviceDeleteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-same-name-delete-")
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

    def _delete(self, window, group, device_data):
        """group の R を削除し、確認に「はい」と答えたことにする。(確認, 警告) を返す"""
        from PyQt6.QtWidgets import QMessageBox
        with mock.patch("ui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes) as question, \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            if device_data is None:
                window._on_device_delete(group, "R")
            else:
                window._on_device_delete(group, "R", dict(device_data))
        return question, warn

    def _hosts(self, window, group):
        return [(d["name"], d["host"])
                for d in window.config_manager.get_group(group)["devices"]]

    def test_the_other_device_can_be_deleted(self):
        window = self._window()

        question, warn = self._delete(window, "G1", G1R)

        warn.assert_not_called()
        question.assert_called_once()
        self.assertEqual(self._hosts(window, "G1"), [],
                         "G2/R のタブのせいで G1/R を削除できない")
        self.assertEqual(self._hosts(window, "G2"), [("R", "192.0.2.12")])
        self.assertEqual(window.device_info.get("R"), G2R,
                         "開いているセッション（G2/R）の再接続用の写しまで消した")

    def test_the_open_sessions_own_device_is_still_refused(self):
        """対照: タブを開いている G2/R そのものの削除は、確認を出さずに断る"""
        window = self._window()

        question, warn = self._delete(window, "G2", G2R)

        warn.assert_called_once()
        self.assertIn("'R' のタブを開いている間は、削除できません。",
                      warn.call_args[0][2])
        question.assert_not_called()
        self.assertEqual(self._hosts(window, "G2"), [("R", "192.0.2.12")])
        self.assertEqual(window.device_info.get("R"), G2R)

    def test_unknown_targets_are_still_refused(self):
        """対照: 写しが無いタブ、項目の機器データが無い呼び出しは、今までどおり断る"""
        for label, session, device_data in (("写しが無い", None, G1R),
                                            ("機器データが無い", G2R, None)):
            with self.subTest(label):
                window = self._window(session=session)

                question, warn = self._delete(window, "G1", device_data)

                warn.assert_called_once()
                question.assert_not_called()
                self.assertEqual(self._hosts(window, "G1"), [("R", "192.0.2.11")])


if __name__ == "__main__":
    unittest.main()
