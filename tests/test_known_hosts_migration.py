"""旧 ~/.terminal-tool/known_hosts の引き継ぎが、黙って既知ホストを
捨てないことを検証する。

app_data_dir() は引き継ぎのコピーを except Exception: pass で潰し、
空の保存先をそのまま返していた。_setup_host_keys は known_hosts が
無ければ _TofuHostKeyPolicy を設定し、そのポリシーは鍵を保存するだけ
で何も聞かない。つまり「失敗しても TOFU の確認が再度出るだけ」という
但し書きは事実ではなく、既知の機器が確認なしで受け入れられる。
既知ホスト鍵を読めないときは接続を中止する、という方針の迂回路に
なっている。

さらに、引き継ぎ済みかどうかを「新しい known_hosts があるか」で
判定していたため、一度 TOFU で新しいファイルが作られると二度と
やり直されず、旧い鍵は恒久的に捨てられていた。
"""
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

OLD_ENTRY = "192.0.2.1 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOldKeyExample\n"
TOFU_ENTRY = "192.0.2.9 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAANewKeyExample\n"


class KnownHostsMigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="netbelt-home-"))
        old_dir = self.home / ".terminal-tool"
        old_dir.mkdir()
        (old_dir / "known_hosts").write_text(OLD_ENTRY, encoding="utf-8")
        self.new_kh = self.home / ".netbelt" / "known_hosts"

    def _this_home(self, broken=False):
        """一時ホームを見せる文脈を返す。

        broken=True のときは、引き継ぎの書き込みだけが失敗する状況を作る。
        コピーで書くか、一時ファイルを差し替えて書くかは実装次第なので、
        どちらの口も塞ぐ。
        """
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(Path, "home",
                                              return_value=self.home))
        if broken:
            stack.enter_context(mock.patch(
                "shutil.copy2", side_effect=PermissionError(13, "denied")))
            stack.enter_context(mock.patch(
                "core.config_manager.os.replace",
                side_effect=PermissionError(13, "denied")))
        return stack

    def _app_data_dir(self, broken=False):
        """この一時ホームで app_data_dir() を呼ぶ。"""
        from core import config_manager
        with self._this_home(broken=broken) as _:
            return config_manager.app_data_dir()

    def _warning(self):
        from core import config_manager
        return config_manager.take_known_hosts_import_warning()

    def test_an_import_is_retried_after_tofu_created_the_file(self):
        """新しい known_hosts が先にできていても旧い鍵を捨てないこと。"""
        self.new_kh.parent.mkdir(parents=True, exist_ok=True)
        self.new_kh.write_text(TOFU_ENTRY, encoding="utf-8")

        self._app_data_dir()

        saved = self.new_kh.read_text(encoding="utf-8")
        self.assertIn("192.0.2.1", saved,
                      "旧 known_hosts の鍵が捨てられている: %r" % saved)
        self.assertIn("192.0.2.9", saved, "TOFU で保存された鍵が消えている")

    def test_a_failed_import_is_not_swallowed(self):
        """引き継げなかった事実が残ること。"""
        self._app_data_dir(broken=True)

        warning = self._warning()
        self.assertIsNotNone(warning, "引き継ぎの失敗を握り潰している")
        self.assertIn("known_hosts", warning)

    def test_a_failed_import_reaches_the_user(self):
        """引き継げなかったことが画面に出ること。"""
        from core.ssh_connection import SSHConnection
        conn = SSHConnection("192.0.2.1", 22, "admin", password="pw")
        output = []
        conn.output_received.connect(output.append)
        with self._this_home(broken=True):
            conn._setup_host_keys(mock.Mock())

        self.assertTrue(any("known_hosts" in o for o in output),
                        "引き継ぎの失敗を利用者に知らせていない: %s" % output)

    def test_a_successful_import_says_nothing(self):
        """対照: 引き継げたときは黙っていること。"""
        self._app_data_dir()

        self.assertIn("192.0.2.1", self.new_kh.read_text(encoding="utf-8"))
        self.assertIsNone(self._warning())

    def test_an_imported_file_is_not_imported_again(self):
        """対照: 引き継ぎ済みなら、利用者が消した行を復活させないこと。"""
        self._app_data_dir()
        self.new_kh.write_text("", encoding="utf-8")

        self._app_data_dir()

        self.assertEqual(self.new_kh.read_text(encoding="utf-8"), "",
                         "引き継ぎ済みの鍵を書き戻している")
        self.assertIsNone(self._warning())


if __name__ == "__main__":
    unittest.main()
