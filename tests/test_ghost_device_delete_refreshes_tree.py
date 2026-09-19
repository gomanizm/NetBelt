"""接続先リストにだけ残った機器を、削除の操作で画面から消せることを検証する。

グループ・プリセットと同じ袋小路が、機器にも残っていた。
remove_device() が「対象が無ければ保存せず False」になったことで、ツリーには
残っているが設定からは消えている機器を削除しようとすると、
「機器の削除に失敗しました。」と出るだけで行が残り続ける。直す前は削除が
「成功」していたのでツリーを作り直しており、案内は誤りだったが表示からは
消えていた（グループについては R2b-main-b で、一覧に残ったままになるのは
直すべき、と判断されている）。

修正: _on_device_delete は、削除を頼む前にその機器が本当にグループにあるかを
見ておき、失敗したときのうち「元から無かった」場合だけ _load_devices() で
ツリーを設定に合わせる。保存に失敗しただけの経路は remove_device が
メモリを巻き戻すので、ツリーは設定と一致したまま。作り直すと畳んでいた
グループが開き直るだけなので、そちらは今までどおり作り直さない。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

DEVICE = {
    "name": "スイッチB", "host": "192.0.2.2", "port": 22, "protocol": "ssh",
    "username": "admin", "password": "", "ssh_key": "", "macros": [],
}


def _device_labels(window, group_name):
    """そのグループの下に並んでいる項目の表示名を返す。"""
    root = window.device_tree.tree.invisibleRootItem()
    for i in range(root.childCount()):
        group = root.child(i)
        if group.text(0) == group_name:
            return [group.child(j).text(0) for j in range(group.childCount())]
    return []


class GhostDeviceDeleteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = tempfile.mkdtemp(prefix="netbelt-ghost-device-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            w = MainWindow()
        self.addCleanup(w.deleteLater)
        return w

    def _window_with_a_device(self):
        w = self._window()
        self.assertTrue(w.config_manager.add_device("Default", dict(DEVICE)))
        w._load_devices()
        self.assertIn("スイッチB (192.0.2.2)", _device_labels(w, "Default"),
                      "前提: ツリーに並んでいない")
        return w

    def _delete(self, window, group_name="Default", device_name="スイッチB"):
        from PyQt6.QtWidgets import QMessageBox
        with mock.patch("ui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window._on_device_delete(group_name, device_name)
        return warn

    def test_a_device_left_only_in_the_tree_disappears_from_the_tree(self):
        w = self._window_with_a_device()
        w.config_manager.remove_device("Default", "スイッチB")
        self.assertEqual(w.config_manager.get_group("Default")["devices"], [],
                         "前提: 設定から消えていない")

        warn = self._delete(w)

        self.assertNotIn("スイッチB (192.0.2.2)", _device_labels(w, "Default"),
                         "設定に無い機器を表示から消せない")
        warn.assert_called_once()
        self.assertIn("失敗", warn.call_args[0][2])

    def test_a_save_failure_does_not_rebuild_the_tree(self):
        """対照: 保存だけ失敗した経路は、これまでどおり作り直さないこと。"""
        from PyQt6.QtWidgets import QMessageBox
        w = self._window_with_a_device()
        w.config_manager.save_config = mock.Mock(return_value=False)
        with mock.patch("ui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn, \
                mock.patch.object(w, "_load_devices") as reload_tree:
            w._on_device_delete("Default", "スイッチB")

        warn.assert_called_once()
        reload_tree.assert_not_called()
        self.assertEqual(
            [d["name"] for d in w.config_manager.get_group("Default")["devices"]],
            ["スイッチB"], "メモリが巻き戻っていない")

    def test_an_existing_device_is_still_deleted(self):
        """対照: 設定にある機器は、これまでどおり消えること。"""
        w = self._window_with_a_device()
        w.status_bar.clearMessage()

        warn = self._delete(w)

        warn.assert_not_called()
        self.assertEqual(_device_labels(w, "Default"), [])
        self.assertIn("削除しました", w.status_bar.currentMessage())


if __name__ == "__main__":
    unittest.main()
