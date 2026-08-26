"""編集した接続情報が、開いているタブの再接続に届くことを検証する。

実機で踏んだ経路:

  正しい鍵で接続 → 切断 → 機器を編集して、存在しない鍵のパスを指定
  → ターミナルで Enter を押して再接続 → **繋がってしまう**

再接続は config ではなく device_info の写しを見ており、その写しが
編集で更新されていなかった。鍵に限らず、ユーザー名・パスワード・ホストを
変えても同じで、古い接続情報のまま繋がり続ける。パスワードを変えて
締め出したつもりの相手が、そのタブからは入れることになる。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


class DeviceEditReachesReconnectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 破棄済みウィジェットへのシグナル配送でプロセスごと落ちるため保持する
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _window(self):
        from ui.main_window import MainWindow
        with mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            window = MainWindow()
        type(self)._windows.append(window)
        return window

    def _old(self):
        return {"name": "Ubuntu0", "host": "192.0.2.10", "port": 22,
                "protocol": "ssh", "username": "cisco", "password": "",
                "ssh_key": r"C:\keys\netbelt_test", "macros": []}

    def _edit(self, window, old, new):
        """編集ダイアログで new を返させ、編集処理を通す。"""
        dialog = mock.Mock()
        dialog.exec.return_value = 1          # Accepted
        dialog.get_device_data.return_value = new
        dialog.get_selected_group.return_value = "Default"
        dialog.group_combo.findText.return_value = 0

        from PyQt6.QtWidgets import QDialog
        with mock.patch("ui.main_window.DeviceDialog", return_value=dialog), \
             mock.patch("ui.main_window.QDialog", QDialog), \
             mock.patch.object(window, "_load_devices"), \
             mock.patch.object(window.config_manager, "get_groups",
                               return_value=[{"name": "Default"}]), \
             mock.patch.object(window.config_manager, "remove_device",
                               return_value=True), \
             mock.patch.object(window.config_manager, "add_device",
                               return_value=True):
            dialog.exec.return_value = QDialog.DialogCode.Accepted
            window._on_device_edit("Default", old)

    def test_a_changed_key_reaches_the_reconnect(self):
        """鍵を差し替えたら、再接続もその鍵を使うこと。"""
        window = self._window()
        old = self._old()
        window.device_info["Ubuntu0"] = old

        new = dict(old, ssh_key=r"C:\keys\netbelt_test2")
        self._edit(window, old, new)

        self.assertEqual(window.device_info["Ubuntu0"]["ssh_key"],
                         r"C:\keys\netbelt_test2",
                         "再接続が古い鍵を使い続ける")

    def test_a_changed_password_reaches_the_reconnect(self):
        """パスワードを変えたら、古いもので繋がらないこと。"""
        window = self._window()
        old = dict(self._old(), ssh_key="", password="old-secret")
        window.device_info["Ubuntu0"] = old

        new = dict(old, password="new-secret")
        self._edit(window, old, new)

        self.assertEqual(window.device_info["Ubuntu0"]["password"],
                         "new-secret",
                         "締め出したはずの古いパスワードで繋がり続ける")

    def test_renaming_does_not_leave_the_old_entry(self):
        """名前を変えたら、古い名前の写しを残さないこと。

        残すと、開いたままのタブがどこにも無い機器へ繋ぎにいく。
        """
        window = self._window()
        old = self._old()
        window.device_info["Ubuntu0"] = old

        new = dict(old, name="Ubuntu1")
        self._edit(window, old, new)

        self.assertNotIn("Ubuntu0", window.device_info,
                         "古い名前の接続情報が残っている")
        self.assertIn("Ubuntu1", window.device_info)

    def test_a_device_that_is_not_open_is_left_alone(self):
        """繋いでいない機器の編集で、他の写しを触らないこと。"""
        window = self._window()
        other = dict(self._old(), name="Cat8000v")
        window.device_info["Cat8000v"] = other

        old = self._old()
        self._edit(window, old, dict(old, ssh_key="changed"))

        self.assertEqual(window.device_info, {"Cat8000v": other})

    def test_deleting_a_device_forgets_how_to_reach_it(self):
        """削除した機器の接続情報を残さないこと。

        残っていると、開いたままのタブで Enter を押すだけで、
        消したはずの機器へ繋がる。
        """
        from PyQt6.QtWidgets import QMessageBox
        window = self._window()
        window.device_info["Ubuntu0"] = self._old()

        with mock.patch("ui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
             mock.patch.object(window, "_load_devices"), \
             mock.patch.object(window.config_manager, "remove_device",
                               return_value=True):
            window._on_device_delete("Default", "Ubuntu0")

        self.assertNotIn("Ubuntu0", window.device_info,
                         "消した機器の接続情報が残っている")


class PanelBandContrastTest(unittest.TestCase):
    """帯の文字が、実際の配色で読めることを検証する。

    1.1.1 で足した接続先の表示は、ダークモードで背景に溶けて消えた。
    一度目の修正はパレットの AlternateBase を背景に、Text を文字色に
    採ったが、それでも消えたままだった。このマシンで測ると理由が出た:

        colorScheme      Dark
        Window           #1e1e1e
        AlternateBase    #ffffff   ← ダークなのに白
        Text             #ffffff

    役割ごとの値が互いに整合している保証は無い。下の WINDOWS11_DARK は
    その実測値そのもので、一度目の修正はこれで落ちる。
    """

    # Windows 11 / Qt6 / windows11 スタイル / システムはダーク の実測値
    WINDOWS11_DARK = {
        "Window": "#1e1e1e", "WindowText": "#ffffff",
        "Base": "#2d2d2d", "AlternateBase": "#ffffff",
        "Text": "#ffffff", "Button": "#3c3c3c",
        "ButtonText": "#ffffff", "Mid": "#282828",
        "PlaceholderText": "#ffffff",
    }

    # 比較用。素直な明るい配色
    PLAIN_LIGHT = {
        "Window": "#f0f0f0", "WindowText": "#000000",
        "Base": "#ffffff", "AlternateBase": "#f7f7f7",
        "Text": "#000000", "Button": "#e1e1e1",
        "ButtonText": "#000000", "Mid": "#a0a0a0",
        "PlaceholderText": "#7f7f7f",
    }

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _luminance(self, name):
        """#rrggbb の相対輝度（0-1）。"""
        r, g, b = (int(name[i:i + 2], 16) / 255 for i in (1, 3, 5))

        def channel(c):
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

        return (0.2126 * channel(r) + 0.7152 * channel(g)
                + 0.0722 * channel(b))

    def _contrast(self, a, b):
        la, lb = self._luminance(a), self._luminance(b)
        hi, lo = max(la, lb), min(la, lb)
        return (hi + 0.05) / (lo + 0.05)

    def _panel(self, colours):
        """その配色をアプリへ適用した状態でパネルを作る。

        スタイルは __init__ で組み立てられるので、あとから
        パレットを差し替えても実際の表示は変わらない。本番と
        同じ順で作る。
        """
        from PyQt6.QtGui import QColor, QPalette
        from ui.sftp_panel import SFTPPanel

        palette = QPalette()
        for role, value in colours.items():
            palette.setColor(getattr(QPalette.ColorRole, role),
                             QColor(value))
        previous = self.app.palette()
        self.app.setPalette(palette)
        self.addCleanup(self.app.setPalette, previous)
        return SFTPPanel()

    def _colours(self, style):
        import re
        bg = re.search(r"background-color:\s*(#[0-9a-fA-F]{6})", style)
        fg = re.search(r"(?<!-)\bcolor:\s*(#[0-9a-fA-F]{6})", style)
        self.assertIsNotNone(bg, "背景色が無い: %r" % style)
        self.assertIsNotNone(fg, "文字色が無い: %r" % style)
        return bg.group(1), fg.group(1)

    def test_the_band_is_readable_on_this_machines_dark_theme(self):
        """実測した配色で読めること。

        AlternateBase と Text をそのまま使うと、ここが
        白地に白（コントラスト 1.0:1）になる。
        """
        panel = self._panel(self.WINDOWS11_DARK)

        for name, label in (("接続先", panel.target_label),
                            ("パス", panel.path_label)):
            with self.subTest(label=name):
                bg, fg = self._colours(label.styleSheet())
                ratio = self._contrast(bg, fg)
                self.assertGreater(
                    ratio, 4.5,
                    "%s の文字が読めない（%s / %s = %.1f:1）"
                    % (name, fg, bg, ratio))

    def test_the_band_is_readable_on_a_light_theme(self):
        panel = self._panel(self.PLAIN_LIGHT)

        bg, fg = self._colours(panel.target_label.styleSheet())
        ratio = self._contrast(bg, fg)
        self.assertGreater(ratio, 4.5,
                           "明るい配色で読めない（%s / %s = %.1f:1）"
                           % (fg, bg, ratio))

    def test_the_band_stands_out_from_the_panel(self):
        """帯だと分かること。地と同じ色では帯の意味がない。"""
        for name, colours in (("ダーク", self.WINDOWS11_DARK),
                              ("ライト", self.PLAIN_LIGHT)):
            with self.subTest(theme=name):
                panel = self._panel(colours)
                bg, _fg = self._colours(panel.target_label.styleSheet())
                self.assertNotEqual(bg.lower(), colours["Window"].lower(),
                                    "帯が地と同じ色")

    def test_the_hint_is_readable_too(self):
        """未接続のときの案内文も読めること。"""
        import re
        panel = self._panel(self.WINDOWS11_DARK)

        style = panel.hint_label.styleSheet()
        fg = re.search(r"color:\s*(#[0-9a-fA-F]{6})", style)
        self.assertIsNotNone(fg, "文字色が無い: %r" % style)
        ratio = self._contrast(self.WINDOWS11_DARK["Window"], fg.group(1))
        self.assertGreater(ratio, 4.5,
                           "案内文が読めない（%s / %s = %.1f:1）"
                           % (fg.group(1), self.WINDOWS11_DARK["Window"],
                              ratio))

    def test_no_hard_coded_background_is_left_in_the_panel(self):
        """パネルの中に、決め打ちの背景色を残さないこと。"""
        import io
        source = io.open("src/ui/sftp_panel.py", encoding="utf-8").read()
        for line in source.split("\n"):
            if "background-color: #" in line:
                self.fail("決め打ちの背景色が残っている: %s" % line.strip())


if __name__ == "__main__":
    unittest.main()
