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

        出ていないと、送り先が違っても気づけない。一覧が届いたあとも
        残ることを見る。以前は Mock のせいで一覧が来ず、_update_file_list
        が上書きする経路を通っていなかった（偽のグリーン）。
        """
        from PyQt6.QtWidgets import QApplication
        window = self._two_devices()
        self._switch(window, "router-A")

        self.assertIn("router-A", window.sftp_panel.path_label.text(),
                      "機器名が出ていない: %r"
                      % window.sftp_panel.path_label.text())

        # 実際の一覧到着を通す（本番ではここで上書きされていた）
        window.sftp_panel._update_file_list([])
        QApplication.processEvents()
        self.assertIn("router-A", window.sftp_panel.path_label.text(),
                      "一覧が届くと機器名が消える: %r"
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


class SftpPanelStaleSignalTest(unittest.TestCase):
    """切り替えたあと、前の機器の通知でパネルが汚れないことを検証する。

    Mock では再現できないので、本物の SFTPManager のシグナルを使う。
    引数なしの disconnect() は「そのシグナルの全接続」を外すため、
    MainWindow が張ったエラー監視まで消えていた。逆に clear() は
    接続を外さず参照だけ捨てていたので、旧機器の一覧が遅れて届くと
    空にしたはずのパネルが埋め直された。
    """

    @classmethod
    def setUpClass(cls):
        import os as _os
        _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from unittest import mock
        # 失敗経路がモーダルを出すと、offscreen では誰も閉じられない
        for name in ("critical", "warning", "information"):
            patcher = mock.patch("ui.sftp_panel.QMessageBox." + name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _attached(self):
        """本物のマネージャを繋いだパネルを返す。"""
        from unittest import mock
        from core.sftp_manager import SFTPManager
        from ui.sftp_panel import SFTPPanel

        panel = SFTPPanel()
        manager = SFTPManager()
        manager.is_connected = True
        manager.sftp_client = mock.Mock()
        manager.sftp_client.listdir_attr.return_value = []
        panel.set_sftp_manager(manager, "old-device", "192.0.2.10")
        return panel, manager

    def _entry(self, name):
        return {"name": name, "size": 1, "is_dir": False,
                "permissions": "-rw-r--r--", "modified": "now"}

    def test_a_late_listing_does_not_refill_a_cleared_panel(self):
        """clear した後に旧機器の一覧が届いても、表示を戻さないこと。"""
        from PyQt6.QtWidgets import QApplication
        panel, manager = self._attached()

        panel.clear()
        manager.file_list_ready.emit([self._entry("OLD.cfg")])
        QApplication.processEvents()

        self.assertEqual(panel.model.rowCount(), 0,
                         "旧機器の一覧でパネルが埋め直された")
        self.assertEqual(panel.target_label.text(), panel.NO_TARGET_TEXT)

    def test_a_late_listing_does_not_leak_into_the_next_device(self):
        """次の機器へ切り替えた後も、旧機器の一覧が入り込まないこと。

        これが起きると、別の機器の中身を見ながらファイルを落とすことになる。
        """
        from unittest import mock
        from PyQt6.QtWidgets import QApplication
        from core.sftp_manager import SFTPManager
        panel, old = self._attached()

        new = SFTPManager()
        new.is_connected = True
        new.sftp_client = mock.Mock()
        new.sftp_client.listdir_attr.return_value = []
        panel.set_sftp_manager(new, "new-device", "192.0.2.11")

        old.file_list_ready.emit([self._entry("OLD.cfg")])
        QApplication.processEvents()

        names = [panel.model.item(r, 0).text()
                 for r in range(panel.model.rowCount())]
        self.assertNotIn("OLD.cfg", names,
                         "旧機器の一覧が新しい機器の表示に混ざった")

    def test_switching_keeps_someone_elses_error_watch(self):
        """切り替えで、他が張ったエラー監視を外さないこと。

        MainWindow は機器ごとに error_occurred を監視している。
        引数なしの disconnect() はそれごと外すので、一度タブを移ると
        以後その機器の SFTP エラーがどこにも出なくなる。
        """
        from unittest import mock
        from PyQt6.QtWidgets import QApplication
        from core.sftp_manager import SFTPManager
        panel, manager = self._attached()

        seen = []
        manager.error_occurred.connect(seen.append)   # MainWindow の代わり

        other = SFTPManager()
        other.is_connected = True
        other.sftp_client = mock.Mock()
        other.sftp_client.listdir_attr.return_value = []
        panel.set_sftp_manager(other, "new-device", "192.0.2.11")

        manager.error_occurred.emit("転送に失敗しました")
        QApplication.processEvents()

        self.assertEqual(seen, ["転送に失敗しました"],
                         "外部のエラー監視まで外している")


if __name__ == "__main__":
    unittest.main()
