"""手編集の config にある「コンソール接続」グループが、COM の更新で一覧から消えないことを検証する。

何が起きていたか（実測、441ea02）: config.json を手で編集して「コンソール接続」
という名前のグループ（rtr-a 192.0.2.21）を置き、COM3 がある状態で読み込むと、
一覧は [('コンソール接続', ['rtr-a (192.0.2.21)']), ('コンソール接続', ['COM3 …'])]
になる。そこで COM4 を挿すと rtr-a のグループが消え、自動検出のグループが 2 つ
（片方は古い COM3 だけ）残った。全部抜くと、もう無い COM3/COM4 の項目が残った。
COM の無い状態で起動して COM3 を挿したとき、右クリックでボーレートを変えた
ときも、rtr-a のグループが消えた（config は無傷で、作り直すと戻る）。

原因: DeviceTree._find_console_group が表示名「コンソール接続」で探しており、
先に作られる設定のグループを自動検出のグループと取り違えて取り除いていた。
折りたたみ状態を控える _remember_console_expanded も同じ関数を使うので、
自動検出のグループを畳んでも、作り直すと開いた状態に戻った。

利用者の決定（A）: 自動検出で作ったグループの項目に印を付け、探すときは名前
ではなく印で見る。config には触れない。設定のグループは、GUI からのグループの
編集・削除・ドラッグを今までどおり断る（名前で判定したまま）。
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

from PyQt6.QtCore import Qt

sys.path.insert(0, "src")

CONSOLE_GROUP = "コンソール接続"
CONFIG_DEVICE = "rtr-a (192.0.2.21)"
GROUPS = [
    {"name": "Default", "auto_commands": [], "devices": []},
    {"name": CONSOLE_GROUP, "auto_commands": [],
     "devices": [{"name": "rtr-a", "host": "192.0.2.21", "port": 22,
                  "protocol": "ssh"}]},
]


def _port(name):
    return {"port": name, "description": "USB Serial Port"}


def _rows(tree):
    """ツリーの (グループ名, [項目の表示名]) の並び"""
    root = tree.tree.invisibleRootItem()
    rows = []
    for i in range(root.childCount()):
        group = root.child(i)
        rows.append((group.text(0),
                     [group.child(j).text(0) for j in range(group.childCount())]))
    return rows


def _detected_group(tree):
    """自動検出の項目を持つ「コンソール接続」グループ（無ければ None）"""
    root = tree.tree.invisibleRootItem()
    for i in range(root.childCount()):
        group = root.child(i)
        if group.childCount() == 0:
            continue
        data = group.child(0).data(0, Qt.ItemDataRole.UserRole) or {}
        if data.get("source") == "autodetect":
            return group
    return None


class ConfigGroupNamedLikeConsoleGroupTest(unittest.TestCase):
    """設定のグループ名が「コンソール接続」でも、自動検出のグループと取り違えないこと"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _tree(self, ports):
        """ports（試験の中から増減させる）を実機のポート一覧に見立てた DeviceTree"""
        from ui.device_tree import DeviceTree
        patcher = mock.patch("ui.device_tree.list_serial_ports",
                             side_effect=lambda: [dict(p) for p in ports])
        patcher.start()
        self.addCleanup(patcher.stop)
        tree = DeviceTree()
        tree.stop_serial_monitor()     # 定期確認は試験の中から明示的に呼ぶ
        self.addCleanup(tree.deleteLater)
        tree.load_from_config(GROUPS)
        return tree

    def assertConfigGroupKept(self, tree, detected):
        """設定のグループが残り、自動検出のグループが実際のポートと一致すること"""
        rows = _rows(tree)
        self.assertIn((CONSOLE_GROUP, [CONFIG_DEVICE]), rows,
                      "設定のグループが一覧から消えた: %r" % (rows,))
        shown = [items for name, items in rows
                 if name == CONSOLE_GROUP and items != [CONFIG_DEVICE]]
        self.assertEqual(shown, [detected] if detected else [],
                         "自動検出のグループが実際のポートと合わない: %r" % (rows,))

    def test_the_config_group_survives_ports_coming_and_going(self):
        ports = [_port("COM3")]
        tree = self._tree(ports)
        self.assertConfigGroupKept(tree, ["COM3 - USB Serial Port"])

        ports.append(_port("COM4"))
        with redirect_stdout(io.StringIO()):
            tree._check_serial_ports()
        self.assertConfigGroupKept(
            tree, ["COM3 - USB Serial Port", "COM4 - USB Serial Port"])

        ports.clear()
        with redirect_stdout(io.StringIO()):
            tree._check_serial_ports()
        self.assertConfigGroupKept(tree, [])

    def test_the_config_group_survives_a_port_plugged_in_later(self):
        ports = []
        tree = self._tree(ports)
        self.assertConfigGroupKept(tree, [])

        ports.append(_port("COM3"))
        with redirect_stdout(io.StringIO()):
            tree._check_serial_ports()
        self.assertConfigGroupKept(tree, ["COM3 - USB Serial Port"])

    def test_the_config_group_survives_a_baudrate_change(self):
        tree = self._tree([_port("COM3")])

        with redirect_stdout(io.StringIO()):
            tree._set_baudrate("COM3", 115200)

        self.assertConfigGroupKept(tree, ["COM3 - USB Serial Port"])
        data = _detected_group(tree).child(0).data(0, Qt.ItemDataRole.UserRole)
        self.assertEqual(data["baudrate"], 115200, "前提: ボーレートが反映された")

    def test_a_collapsed_detected_group_stays_collapsed_across_a_reload(self):
        """自動検出のグループを畳んだ状態が、作り直しで開き直らないこと"""
        tree = self._tree([_port("COM3")])
        _detected_group(tree).setExpanded(False)

        tree.load_from_config(GROUPS)

        self.assertFalse(_detected_group(tree).isExpanded(),
                         "設定のグループの状態を控えて、畳んだ自動検出のグループが開き直った")

    def test_the_config_group_still_has_no_group_edit_or_delete(self):
        """設定のグループのグループ編集・削除は、今までどおりメニューに出さないこと"""
        tree = self._tree([_port("COM3")])
        root = tree.tree.invisibleRootItem()
        config_group = next(
            root.child(i) for i in range(root.childCount())
            if root.child(i).text(0) == CONSOLE_GROUP
            and _detected_group(tree) is not root.child(i))
        texts = []

        def fake_exec(menu, *args, **kwargs):
            texts.extend(a.text() for a in menu.actions())
            return None

        with mock.patch("PyQt6.QtWidgets.QMenu.exec", fake_exec):
            tree._show_group_menu(tree.tree.visualItemRect(config_group).center(),
                                  config_group)

        self.assertNotIn("グループを編集", texts)
        self.assertNotIn("グループを削除", texts)
        self.assertIn("接続先リストを非表示", texts, "前提: メニューは出ている")


if __name__ == "__main__":
    unittest.main()
