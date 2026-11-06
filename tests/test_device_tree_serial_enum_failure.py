# -*- coding: utf-8 -*-
"""シリアルポートの列挙が失敗しても、接続先リストが動き続けることを検証する。

何が起きていたか。DeviceTree はツリーを組み直すたびと 1 秒ごとのタイマーで
list_serial_ports() を try 無しで呼んでいた。pyserial の列挙は、ドライバーや
レジストリ項目の壊れた機器が 1 台あるだけで OSError を投げる。起動時は
MainWindow() が例外で止まって NetBelt が起動せず、動作中は 1 秒ごとに
「予期しないエラー」のモーダルが出続けていた。

失敗したときは前回の一覧を保ち、同じ理由は 1 回だけ記録する。
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

PORTS = [{"port": "COM7", "description": "USB Serial"}]


def _fail(*_args, **_kwargs):
    raise PermissionError(13, "The data is invalid")


class SerialEnumFailureTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _tree(self):
        from ui.device_tree import DeviceTree
        tree = DeviceTree()
        tree.stop_serial_monitor()
        self.addCleanup(tree.deleteLater)
        return tree

    @staticmethod
    def _console_ports(tree):
        group = tree._find_console_group()
        if group is None:
            return []
        return [group.child(i).data(0, 0x0100)["port"]
                for i in range(group.childCount())]

    def test_load_survives_enumeration_failure(self):
        """起動時（ツリーの組み立て）に列挙が失敗しても例外で抜けないこと。"""
        tree = self._tree()
        with mock.patch("ui.device_tree.list_serial_ports", _fail), \
                contextlib.redirect_stdout(io.StringIO()):
            tree.load_from_config([
                {"name": "A", "devices": [{"name": "rtr1",
                                           "host": "192.0.2.1"}]},
            ])
        self.assertEqual(tree.tree.topLevelItem(0).text(0), "A")
        self.assertEqual(self._console_ports(tree), [])

    def test_timer_check_keeps_previous_ports(self):
        """定期確認で列挙が失敗しても例外で抜けず、前回の一覧を保つこと。"""
        tree = self._tree()
        with mock.patch("ui.device_tree.list_serial_ports",
                        return_value=PORTS):
            tree.load_from_config([])
        self.assertEqual(self._console_ports(tree), ["COM7"])

        with mock.patch("ui.device_tree.list_serial_ports", _fail), \
                contextlib.redirect_stdout(io.StringIO()):
            tree._check_serial_ports()
            # 組み直しが走っても一覧は前回のまま
            tree.load_from_config([])
        self.assertEqual(self._console_ports(tree), ["COM7"])
        self.assertEqual(tree._current_serial_ports, {"COM7"})

    def test_same_failure_logged_once(self):
        """同じ理由の失敗は 1 回だけ記録し、直ったあとの失敗はまた記録すること。"""
        tree = self._tree()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with mock.patch("ui.device_tree.list_serial_ports", _fail):
                for _ in range(5):
                    tree._check_serial_ports()
            first = out.getvalue().count("The data is invalid")
            with mock.patch("ui.device_tree.list_serial_ports",
                            return_value=PORTS):
                tree._check_serial_ports()
            with mock.patch("ui.device_tree.list_serial_ports", _fail):
                tree._check_serial_ports()
        self.assertEqual(first, 1)
        self.assertEqual(out.getvalue().count("The data is invalid"), 2)


if __name__ == "__main__":
    unittest.main()
