"""SFTP のダウンロードで、利用者が承認していない保存先を置き換えないことを検証する。

実測（基準 441ea02）:
  パネルは保存ダイアログ（Windows ネイティブ）の上書き確認に任せ、
  download_file(remote, local) へは保存先だけを渡していた。承認したか
  どうかは渡さず、download_file が呼ばれた時点で保存先が在るかどうか
  （os.path.exists）から「ダイアログが上書きを確認した」と推し量っていた
  （src/core/sftp_manager.py:1044-1046）。そのため、ダイアログが「無い」と
  見て確認を出さなかったあと、download_file までの間に別のプロセスが同じ
  名前を作ると「承認済み」と読まれ、転送のあと os.replace で置き換わった。
  getSaveFileName を差し替えて、返す直前に保存先へ PRECIOUS-LOCAL を書くと、
  確認は 0 回、done=['ダウンロード完了: startup.cfg']、errors=[]、保存先の
  中身は b'REMOTE' になった（外の内容は確認なしに消えた）。ダイアログが
  返ってから download_file までは 11.3 us。

利用者の決定（2026-09 の 1.3.2 の仕分け、案 B）:
  保存ダイアログの上書き確認を NetBelt 自前の確認へ置き換え、「いいえ」なら
  保存ダイアログを開き直す（ネイティブの確認で「いいえ」を押したときと同じ）。

直し方:
  getSaveFileName に DontConfirmOverwrite を付け、返った保存先が在れば
  自前で「'名前' が既にあります。上書きしますか？」と訊く。「いいえ」なら
  選んだ名前を初期値にして保存ダイアログを開き直し、取り消されたら何も
  しない。承認したかどうかは download_file(..., overwrite=<承認したか>) で
  そのまま渡す。download_file は overwrite が True なら os.replace、False なら
  os.rename（Windows では宛先があると断る）で確定するので、承認の判断の
  あとに外で作られた保存先も置き換えない。overwrite を省いた呼び出しは
  これまでどおり、呼ばれた時点の有無で推し量る。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

PRECIOUS = b"PRECIOUS-LOCAL"
REMOTE = b"REMOTE"


class DownloadOverwriteConsentTest(unittest.TestCase):
    _keep = []

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.sftp_manager import SFTPManager
        from ui.sftp_panel import SFTPPanel
        self.dir = tempfile.mkdtemp(prefix="netbelt-dl-consent-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.target = os.path.join(self.dir, "startup.cfg")
        m = self.m = SFTPManager()
        m.is_connected = True
        m.current_path = "/flash"
        m.sftp_client = mock.Mock()
        m.sftp_client.get_channel.return_value.closed = False

        def fake_get(remote, local, callback=None):
            with open(local, "wb") as f:
                f.write(REMOTE)
        m.sftp_client.get.side_effect = fake_get
        m.sftp_client.listdir_attr.return_value = []
        self.done, self.errors = [], []
        m.transfer_complete.connect(self.done.append)
        m.error_occurred.connect(self.errors.append)
        self.panel = SFTPPanel()
        # 後片付けで C++ 側が先に消えないよう、テストの間は参照を持ち続ける
        type(self)._keep += [m, self.panel]
        self.panel.set_sftp_manager(m, "router-A", "192.0.2.10")
        self._pump(lambda: False, 0.1)

    def _pump(self, check, seconds=3.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if check():
                return True
            time.sleep(0.01)
        return False

    def _read(self, path=None):
        with open(path or self.target, "rb") as f:
            return f.read()

    def _write(self, data, path=None):
        with open(path or self.target, "wb") as f:
            f.write(data)

    def _download(self, dialog, replies=()):
        """保存ダイアログと確認を差し替えてダウンロードを始め、終わるまで待つ

        Args:
            dialog: getSaveFileName の side_effect
            replies: 確認に順に返す答え（True=はい）
        Returns:
            (保存ダイアログの mock, 確認の mock)
        """
        from ui import sftp_panel as mod
        yes = mod.QMessageBox.StandardButton.Yes
        no = mod.QMessageBox.StandardButton.No
        answers = [yes if r else no for r in replies]
        with mock.patch.object(mod.QFileDialog, "getSaveFileName",
                               side_effect=dialog) as chooser, \
                mock.patch.object(mod.QMessageBox, "question",
                                  side_effect=answers) as question, \
                mock.patch.object(mod.QMessageBox, "warning"):
            self.panel._on_download_selected({"name": "startup.cfg", "is_dir": False})
            self._pump(lambda: self.done or self.errors, 2.0)
        return chooser, question

    def test_a_file_created_during_the_dialog_is_not_replaced_without_asking(self):
        """保存ダイアログが「無い」と見たあとに外で作られた保存先を、訊かずに置き換えないこと。"""
        results = [(self.target, ""), ("", "")]

        def dialog(*args, **kwargs):
            if not os.path.exists(self.target):
                self._write(PRECIOUS)      # ダイアログの確認のあとに外で作られる
            return results.pop(0)
        chooser, question = self._download(dialog, replies=[False])
        self.assertEqual(self._read(), PRECIOUS, "承認していない上書きで外の内容が消えた")
        self.assertEqual(question.call_count, 1, "上書きの確認が出ていない")
        self.assertEqual(self.done, [])

    def test_no_reopens_the_save_dialog_with_the_chosen_name(self):
        """「いいえ」なら、選んだ名前を初期値にして保存ダイアログを開き直すこと。"""
        self._write(PRECIOUS)
        other = os.path.join(self.dir, "startup-new.cfg")
        results = [(self.target, ""), (other, "")]
        chooser, question = self._download(lambda *a, **kw: results.pop(0),
                                           replies=[False])
        self.assertEqual(chooser.call_count, 2, "保存ダイアログを開き直していない")
        self.assertEqual(chooser.call_args_list[1][0][2], self.target,
                         "開き直すときは、いま選んだ名前を初期値にする")
        self.assertEqual(question.call_count, 1, "新しい名前では訊かない")
        self.assertEqual(self._read(), PRECIOUS, "断った保存先に触れた")
        self.assertEqual(self._read(other), REMOTE, self.errors)

    def test_the_native_overwrite_prompt_is_turned_off(self):
        """保存ダイアログ自身の上書き確認は切り、確認はこちらの一か所で行うこと。"""
        from ui import sftp_panel as mod
        chooser, _ = self._download(lambda *a, **kw: ("", ""))
        options = chooser.call_args.kwargs.get("options")
        self.assertIsNotNone(options, "保存ダイアログへ options を渡していない")
        self.assertTrue(options & mod.QFileDialog.Option.DontConfirmOverwrite)

    def test_a_file_created_after_the_consent_is_not_replaced(self):
        """在る／無いを判断したあと、download_file までに作られた保存先も置き換えないこと。"""
        orig = self.m.download_file

        def create_then_download(*args, **kwargs):
            self._write(PRECIOUS)          # 判断のあと・download_file の前
            return orig(*args, **kwargs)
        self.m.download_file = create_then_download
        self._download(lambda *a, **kw: (self.target, ""))
        self.assertEqual(self._read(), PRECIOUS, "承認していない上書きで外の内容が消えた")
        self.assertTrue(any("置き換えていません" in e for e in self.errors), self.errors)

    def test_an_existing_file_is_replaced_when_the_user_agrees(self):
        """対照: 在る保存先は、上書きを承認したら置き換えること。"""
        self._write(b"older")
        chooser, question = self._download(lambda *a, **kw: (self.target, ""),
                                           replies=[True])
        self.assertEqual(self._read(), REMOTE, self.errors)
        question.assert_called_once()
        self.assertIn("startup.cfg", question.call_args[0][2])
        self.assertEqual(chooser.call_count, 1)
        self.assertEqual(self.done, ["ダウンロード完了: startup.cfg"])

    def test_a_new_name_is_saved_without_asking(self):
        """対照: 無い保存先へは、訊かずにそのまま保存すること。"""
        chooser, question = self._download(lambda *a, **kw: (self.target, ""))
        self.assertEqual(self._read(), REMOTE, self.errors)
        question.assert_not_called()
        self.assertEqual(chooser.call_count, 1)

    def test_the_download_is_abandoned_when_the_session_drops_during_the_prompt(self):
        """上書きの確認の最中に切れたら、「はい」でもダウンロードしないこと。"""
        from ui import sftp_panel as mod
        self._write(PRECIOUS)

        def lose_the_session_then_agree(*args, **kwargs):
            self.panel.clear()
            return mod.QMessageBox.StandardButton.Yes
        with mock.patch.object(mod.QFileDialog, "getSaveFileName",
                               return_value=(self.target, "")), \
                mock.patch.object(mod.QMessageBox, "question",
                                  side_effect=lose_the_session_then_agree), \
                mock.patch.object(self.m, "download_file") as download_file:
            self.panel._on_download_selected({"name": "startup.cfg", "is_dir": False})
        download_file.assert_not_called()
        self.assertEqual(self._read(), PRECIOUS)


class DownloadFileOverwriteArgumentTest(unittest.TestCase):
    """download_file が、渡された承認に従って確定の仕方を選ぶこと"""
    _keep = []

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-dl-overwrite-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.target = os.path.join(self.dir, "backup.cfg")

    def _manager(self):
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        m.is_connected = True
        m.sftp_client = mock.Mock()
        m.sftp_client.get_channel.return_value.closed = False

        def fake_get(remote, local, callback=None):
            with open(local, "wb") as f:
                f.write(REMOTE)
        m.sftp_client.get.side_effect = fake_get
        done, errors = [], []
        m.transfer_complete.connect(done.append)
        m.error_occurred.connect(errors.append)
        type(self)._keep.append(m)
        return m, done, errors

    def _wait(self, check, seconds=3.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if check():
                return True
            time.sleep(0.01)
        return False

    def _read(self):
        with open(self.target, "rb") as f:
            return f.read()

    def test_an_existing_target_is_kept_when_overwrite_is_not_granted(self):
        """呼ばれた時点で在っても、承認されていなければ置き換えないこと。"""
        with open(self.target, "wb") as f:
            f.write(PRECIOUS)
        m, done, errors = self._manager()
        m.download_file("/flash/backup.cfg", self.target, overwrite=False)
        self.assertTrue(self._wait(lambda: done or errors), "終わらない")
        self.assertEqual(self._read(), PRECIOUS)
        self.assertEqual(done, [])
        self.assertTrue(any("置き換えていません" in e for e in errors), errors)
        leftovers = [n for n in os.listdir(self.dir) if n.endswith(".netbelt-part")]
        self.assertEqual(len(leftovers), 1, "落とした内容は一時名に残すこと")

    def test_an_existing_target_is_replaced_when_overwrite_is_granted(self):
        """対照: 承認されていれば置き換えること。"""
        with open(self.target, "wb") as f:
            f.write(b"older")
        m, done, errors = self._manager()
        m.download_file("/flash/backup.cfg", self.target, overwrite=True)
        self.assertTrue(self._wait(lambda: done or errors), "終わらない")
        self.assertEqual(self._read(), REMOTE, errors)
        self.assertEqual(done, ["ダウンロード完了: backup.cfg"])


if __name__ == "__main__":
    unittest.main()
