"""匿名 FTP の書き込みを、ログインの許可とは別に選べることを検証する。

「匿名を許可」にチェックを入れると add_anonymous(root_dir,
perm="elradfmwMT") が実行されていた。認証ユーザー用と同じ perm 文字列の
使い回しで、w/d/f/m が含まれるため、資格情報なしでルート配下のファイルを
上書き・削除・改名・フォルダ作成できた。チェックボックスのラベルは
「匿名を許可」だけで、書き込みまで与えることはどこにも書かれていない。

パネルの説明が示すとおり主用途は `copy running-config ftp://…`、つまり
機器がコンフィグを置きに来る経路なので、匿名の書き込み自体は必要な機能
である。問題は「ログインの許可」と「書き込みの許可」が1つのチェック
ボックスに束ねられていたことと、与える権限が用途に対して広すぎたこと。

そこで両者を分け、書き込みを許す場合も置きに来るのに要る最小限に絞る。
削除・改名・フォルダ作成・パーミッション変更は、この用途では要らない。

既に「匿名を許可」で運用している設定を黙って読み取り専用に変えると、
機器からのアップロードがある日から通らなくなる。書き込みの可否を
記録していない古い設定は、これまでどおり書き込みを許した状態で読み込む。
"""
import ftplib
import io
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, "src")


class AnonymousPermissionTest(unittest.TestCase):
    """サーバ側。匿名に与える権限。"""

    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        self.root = tempfile.mkdtemp(prefix="netbelt-ftp-anon-")
        from core.ftp_server import FTPServerManager
        self.m = FTPServerManager()
        patch = unittest.mock.patch(
            "core.firewall.ensure_inbound_allow", return_value=(True, "test stub"))
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.m.stop)

    def _start(self, **kwargs):
        self.assertTrue(self.m.start(port=0, root_dir=self.root,
                                     username="", password="",
                                     anonymous=True, **kwargs))
        time.sleep(0.3)
        return self.m.port

    def _login(self, port):
        f = ftplib.FTP()
        f.connect("127.0.0.1", port, timeout=5)
        f.login()
        self.addCleanup(lambda: f.close())
        return f

    def _place(self, name, body=b"hostname R1"):
        with open(os.path.join(self.root, name), "wb") as fh:
            fh.write(body)

    def test_anonymous_cannot_store_by_default(self):
        """許可した覚えのない書き込みを、既定で通さないこと。"""
        f = self._login(self._start())
        with self.assertRaises(ftplib.error_perm):
            f.storbinary("STOR anon.cfg", io.BytesIO(b"hostname R2"))
        self.assertFalse(os.path.exists(os.path.join(self.root, "anon.cfg")),
                         "既定で匿名の書き込みが通っている")

    def test_anonymous_can_still_retrieve_by_default(self):
        """読み取りはこれまでどおり通ること。"""
        self._place("running.cfg")
        f = self._login(self._start())
        buf = io.BytesIO()
        f.retrbinary("RETR running.cfg", buf.write)
        self.assertEqual(buf.getvalue(), b"hostname R1")

    def test_anonymous_cannot_delete_by_default(self):
        self._place("secret.cfg")
        f = self._login(self._start())
        with self.assertRaises(ftplib.error_perm):
            f.delete("secret.cfg")
        self.assertTrue(os.path.exists(os.path.join(self.root, "secret.cfg")),
                        "既定で匿名の削除が通っている")

    def test_anonymous_can_store_when_write_is_turned_on(self):
        """書き込みを明示的に許したら、機器からのアップロードが通ること。"""
        f = self._login(self._start(anonymous_write=True))
        f.storbinary("STOR anon.cfg", io.BytesIO(b"hostname R2"))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "anon.cfg")))

    def test_anonymous_write_does_not_grant_deleting(self):
        """書き込みを許しても、削除まで許さないこと。"""
        self._place("secret.cfg")
        f = self._login(self._start(anonymous_write=True))
        with self.assertRaises(ftplib.error_perm):
            f.delete("secret.cfg")
        self.assertTrue(os.path.exists(os.path.join(self.root, "secret.cfg")),
                        "書き込みの許可が削除まで許している")

    def test_anonymous_write_does_not_grant_renaming(self):
        """書き込みを許しても、改名まで許さないこと。"""
        self._place("secret.cfg")
        f = self._login(self._start(anonymous_write=True))
        with self.assertRaises(ftplib.error_perm):
            f.rename("secret.cfg", "gone.cfg")

    def test_anonymous_write_does_not_grant_making_folders(self):
        f = self._login(self._start(anonymous_write=True))
        with self.assertRaises(ftplib.error_perm):
            f.mkd("newdir")


class AnonymousWriteSwitchTest(unittest.TestCase):
    """パネル側。ログインの許可と書き込みの許可を分けて見せる。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self, settings=None):
        from core.config_manager import ConfigManager
        from ui.ftp_server_panel import FTPServerPanel
        d = tempfile.mkdtemp(prefix="netbelt-ftp-panel-")
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        if settings is not None:
            cm.set_server_settings("ftp_server", settings)
        return FTPServerPanel(config_manager=cm)

    def test_there_is_a_separate_switch_for_anonymous_writing(self):
        panel = self._panel()
        self.assertTrue(hasattr(panel, "anonymous_write_check"),
                        "匿名の書き込みを別に選べない")

    def test_the_switch_says_what_it_grants(self):
        """ラベルを読めば、何を許すのか分かること。"""
        panel = self._panel()
        label = panel.anonymous_write_check.text()
        self.assertIn("書き込み", label,
                      "何を許可するのかラベルから分からない: %r" % label)

    def test_a_fresh_setup_does_not_allow_anonymous_writing(self):
        """新しく使い始めた人には、書き込みを許さない状態で見せること。"""
        panel = self._panel()
        self.assertFalse(panel.anonymous_write_check.isChecked())

    def test_an_existing_anonymous_setup_keeps_writing(self):
        """既に匿名で運用している設定は、書き込みを保ったまま読むこと。

        黙って読み取り専用にすると、機器からのアップロードがある日から
        通らなくなる。書き込みの可否を持っていない古い設定が対象。
        """
        panel = self._panel({"anonymous": True, "port": 21,
                             "root_directory": "./ftp_root"})
        self.assertTrue(panel.anonymous_write_check.isChecked(),
                        "既存の匿名運用が黙って読み取り専用になっている")

    def test_a_saved_choice_is_honoured(self):
        """一度選んだ結果は、そのまま復元すること。"""
        panel = self._panel({"anonymous": True, "anonymous_write": False,
                             "port": 21, "root_directory": "./ftp_root"})
        self.assertFalse(panel.anonymous_write_check.isChecked(),
                         "保存した選択が無視されている")


if __name__ == "__main__":
    unittest.main()
