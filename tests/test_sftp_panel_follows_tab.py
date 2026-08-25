"""SFTP パネルが、表示中のターミナルの機器に追従することを検証する。

複数の機器へ SSH で入ると機器ごとに SFTP セッションができるが、パネルは
接続した瞬間のものを表示したきりで、タブを切り替えても追従しなかった。

  A 接続直後     : ターミナル=router-A  SFTPパネル=router-A
  B 接続直後     : ターミナル=switch-B  SFTPパネル=switch-B
  A のタブへ戻す : ターミナル=router-A  SFTPパネル=switch-B  ← 食い違い

画面上は router-A を見ているのにドロップしたファイルは switch-B へ送られ、
パネルに機器名が出ないので気づく手がかりも無かった。機器へ config を配る
道具として、送り先の取り違えは許容できない。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class SftpPanelFollowsTabTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 作った MainWindow はクラス終了まで保持する（破棄済みウィジェットへの
    # シグナル配送でプロセスごと落ちるため）
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _window(self):
        from unittest import mock
        from ui.main_window import MainWindow
        with mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            window = MainWindow()
        type(self)._windows.append(window)
        return window

    def _manager(self, path):
        from unittest import mock
        manager = mock.Mock()
        manager.current_path = path
        return manager

    def _switch(self, window, name):
        from PyQt6.QtWidgets import QApplication
        tabs = window.terminal_widget.tab_widget
        for i in range(tabs.count()):
            if tabs.tabText(i).replace("● ", "").strip() == name:
                tabs.setCurrentIndex(i)
                break
        else:
            self.fail("タブが見つからない: %s" % name)
        QApplication.processEvents()

    def _two_devices(self):
        window = self._window()
        window.terminal_widget.create_terminal_tab("router-A")
        window.terminal_widget.create_terminal_tab("switch-B")
        window.sftp_managers["router-A"] = self._manager("/home/A")
        window.sftp_managers["switch-B"] = self._manager("/home/B")

        self._switch(window, "router-A")
        window.sftp_panel.set_sftp_manager(
            window.sftp_managers["router-A"], "router-A")
        self._switch(window, "switch-B")
        window.sftp_panel.set_sftp_manager(
            window.sftp_managers["switch-B"], "switch-B")
        return window

    def test_the_panel_follows_the_terminal_tab(self):
        """タブを戻したら、パネルもその機器へ戻ること。"""
        window = self._two_devices()

        self._switch(window, "router-A")

        self.assertEqual(window.sftp_panel.current_device, "router-A",
                         "別の機器を指したまま（送り先を間違える）")
        self.assertEqual(window.terminal_widget.get_current_tab_name(),
                         window.sftp_panel.current_device)

    def test_the_path_names_the_device(self):
        """どの機器を見ているかが画面に出ること。

        出ていないと、送り先が違っても気づけない。
        """
        window = self._two_devices()
        self._switch(window, "router-A")

        self.assertIn("router-A", window.sftp_panel.path_label.text(),
                      "機器名が出ていない: %r"
                      % window.sftp_panel.path_label.text())

    def test_a_device_without_sftp_shows_nothing_stale(self):
        """SFTP を持たない機器へ切り替えたら、前の機器の内容を残さないこと。"""
        window = self._two_devices()
        window.terminal_widget.create_terminal_tab("console-C")

        self._switch(window, "console-C")

        self.assertEqual(window.sftp_panel.current_device, "",
                         "SFTP の無い機器なのに前の接続が残っている")

    def test_switching_back_and_forth_keeps_them_in_step(self):
        """行き来しても食い違わないこと。"""
        window = self._two_devices()
        for name in ("router-A", "switch-B", "router-A", "switch-B"):
            with self.subTest(tab=name):
                self._switch(window, name)
                self.assertEqual(window.sftp_panel.current_device, name)


if __name__ == "__main__":
    unittest.main()
