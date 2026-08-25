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
        from core.ssh_connection import SSHConnection
        window = self._window()
        window.terminal_widget.create_terminal_tab("router-A")
        window.terminal_widget.create_terminal_tab("switch-B")
        window.sftp_managers["router-A"] = self._manager("/home/A")
        window.sftp_managers["switch-B"] = self._manager("/home/B")
        # 接続先を出すために、接続情報も持たせる（例示は RFC 5737）
        window.connections["router-A"] = SSHConnection(
            host="192.0.2.10", port=22, username="admin")
        window.connections["switch-B"] = SSHConnection(
            host="192.0.2.11", port=2222, username="admin")

        # 接続直後の状態は、タブ切替の経路がそのまま作る
        self._switch(window, "router-A")
        self._switch(window, "switch-B")
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

    def test_the_header_names_the_device_and_address(self):
        """上部に、どの機器のどのアドレスへ繋いでいるかを出すこと。

        パスの行に機器名を添えるだけでは、本当にその機器かを判断しづらい。
        ファイルを落とす前に必ず目に入る位置へ出す。
        """
        window = self._two_devices()

        self._switch(window, "router-A")
        header = window.sftp_panel.target_label.text()
        self.assertIn("router-A", header, "機器名が出ていない: %r" % header)
        self.assertIn("192.0.2.10", header, "アドレスが出ていない: %r" % header)

    def test_a_nonstandard_port_is_shown(self):
        """22 番以外のポートは省かないこと（別の機器と紛れる）。"""
        window = self._two_devices()

        self._switch(window, "switch-B")
        header = window.sftp_panel.target_label.text()
        self.assertIn("192.0.2.11:2222", header,
                      "非標準ポートが出ていない: %r" % header)

    def test_the_standard_port_is_not_noise(self):
        """22 番は省くこと（毎回出ても情報にならない）。"""
        window = self._two_devices()

        self._switch(window, "router-A")
        self.assertNotIn(":22", window.sftp_panel.target_label.text())

    def test_the_header_clears_for_a_device_without_sftp(self):
        """SFTP を持たない機器では、前の接続先を出したままにしないこと。"""
        window = self._two_devices()
        window.terminal_widget.create_terminal_tab("console-C")

        self._switch(window, "console-C")

        self.assertEqual(window.sftp_panel.target_label.text(),
                         window.sftp_panel.NO_TARGET_TEXT,
                         "前の機器の接続先が残っている")


if __name__ == "__main__":
    unittest.main()
