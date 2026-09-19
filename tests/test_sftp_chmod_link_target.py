"""シンボリックリンクの権限変更で、何も変えずに OK してもリンク先が 777 にならないことを検証する。

何が起きていたか（実測）:
  一覧の mode は listdir_attr の st_mode、つまりリンク自身（lstat 相当）の
  値で、多くの機器では 0777 になる。権限変更ダイアログはこの値を初期値に
  出し、適用する chmod はリンクをたどって先へ効く。そのため 0600 の設定
  ファイルや /etc/ssh（0755）を指すリンクで、ダイアログを何も変えずに OK
  しただけで、リンク先が 0777 になった（change_permissions(..., 0o777)）。

利用者の決定（2026-09-20）:
  リンクの権限変更では、初期値をリンク先の権限（stat でたどった値）にし、
  ダイアログに『リンク先 <名前> に適用されます』と表示する。初期値から
  何も変えずに OK したときは何も送らない。リンク先を読めない（壊れた
  リンクなど）ときは権限変更を断り、その理由を伝える。

どう実装したか:
  SFTPManager.inspect_link_target が stat でリンク先の mode を、readlink で
  リンク先の名前を読む。パネルはリンクの項目に限ってこれを呼び、mode を
  初期値に、名前をダイアログの文言に使う。入力がその初期値と同じ値なら
  change_permissions を呼ばない。stat が失敗したらマネージャが理由を
  error_occurred で知らせ、パネルはダイアログを開かずに戻る。
  リンクでない項目は、これまでどおり一覧の mode を初期値にする（追加の
  問い合わせもしない）。
"""
import os
import posixpath
import stat
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class FakeAttr:
    """listdir_attr / stat が返す項目の代わり（必要な属性だけ持つ）。"""

    def __init__(self, filename, st_mode, st_size=0, st_mtime=0):
        self.filename = filename
        self.st_mode = st_mode
        self.st_size = st_size
        self.st_mtime = st_mtime


class SftpChmodLinkTargetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls._panels = []

    def _wait(self, box, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if box:
                return True
            time.sleep(0.02)
        return False

    def _manager(self, targets, links=None):
        """/base に項目を並べたマネージャを作る。

        Args:
            targets: パス -> stat が返す mode（例外なら raise する）
            links: パス -> readlink が返す文字列
        """
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        m.sftp_client = mock.Mock()
        m.sftp_client.listdir_attr.return_value = [
            FakeAttr("app.conf", stat.S_IFLNK | 0o777),
            FakeAttr("sshdir", stat.S_IFLNK | 0o777),
            FakeAttr("deadlink", stat.S_IFLNK | 0o777),
            FakeAttr("run.cfg", stat.S_IFREG | 0o644, st_size=11),
        ]

        def follow(path):
            value = targets.get(path)
            if isinstance(value, BaseException):
                raise value
            if value is None:
                raise FileNotFoundError(2, "No such file")
            return FakeAttr(posixpath.basename(path), value)

        def readlink(path):
            if links and path in links:
                return links[path]
            raise IOError("Operation unsupported")

        m.sftp_client.stat.side_effect = follow
        m.sftp_client.readlink.side_effect = readlink
        self.errors = []
        m.error_occurred.connect(self.errors.append)
        return m

    def _listing(self, m):
        box = []
        m.file_list_ready.connect(box.append)
        m.list_directory("/base")
        self.assertTrue(self._wait(box), "file_list_ready が来なかった")
        # 以後の検証で、一覧のための stat と権限変更のための stat を分けて見る。
        # 権限変更のあとの一覧の取り直しも止める（別スレッドで stat が走る）
        m.list_directory = mock.Mock()
        m.sftp_client.stat.reset_mock()
        m.sftp_client.readlink.reset_mock()
        return {e["name"]: e for e in box[0]}

    def _panel(self, m):
        from ui.sftp_panel import SFTPPanel

        panel = SFTPPanel()
        type(self)._panels.append(panel)      # 親より先に捨てない
        panel.set_sftp_manager(m, "rtrA")
        panel._update_file_list([])
        return panel

    def _chmod(self, m, entry, answer=None):
        """ダイアログを開いて、answer（None なら初期値のまま）で OK する。

        Returns:
            ダイアログが開いたら {'label': 文言, 'default': 初期値}、開かなければ {}
        """
        from ui import sftp_panel as mod

        panel = self._panel(m)
        seen = {}

        def dialog(parent, title, label, text="", *args, **kwargs):
            seen['label'] = label
            seen['default'] = text
            return (text if answer is None else answer, True)

        with mock.patch.object(mod.QInputDialog, "getText", side_effect=dialog), \
                mock.patch.object(mod.QMessageBox, "warning") as warning:
            panel._on_chmod_selected(entry, (m, "/base"))
            self.app.processEvents()
        self.warnings = [c[0][2] for c in warning.call_args_list]
        return seen

    def _targets(self):
        return {
            "/base/app.conf": stat.S_IFREG | 0o600,
            "/base/sshdir": stat.S_IFDIR | 0o755,
            "/base/deadlink": FileNotFoundError(2, "No such file"),
        }

    def test_an_unchanged_ok_on_a_link_leaves_the_target_alone(self):
        """0600 のファイルを指すリンクで、何も変えずに OK しても何も送らないこと。"""
        m = self._manager(self._targets(), {"/base/app.conf": "/etc/app.conf"})
        by_name = self._listing(m)

        seen = self._chmod(m, by_name["app.conf"])

        self.assertEqual(seen.get('default'), "0600",
                         "初期値がリンク先の権限になっていない（リンク自身の値のまま）")
        m.sftp_client.chmod.assert_not_called()

    def test_an_unchanged_ok_on_a_link_to_a_directory_sends_nothing(self):
        """/etc/ssh（0755）のようなディレクトリを指すリンクでも同じ。"""
        m = self._manager(self._targets(), {"/base/sshdir": "/etc/ssh"})
        by_name = self._listing(m)

        seen = self._chmod(m, by_name["sshdir"])

        self.assertEqual(seen.get('default'), "0755")
        m.sftp_client.chmod.assert_not_called()

    def test_the_dialog_names_the_link_target(self):
        """ダイアログに、変更がリンク先へ適用されることを名前つきで出すこと。"""
        m = self._manager(self._targets(), {"/base/app.conf": "/etc/app.conf"})
        by_name = self._listing(m)

        seen = self._chmod(m, by_name["app.conf"])

        self.assertIn("リンク先 /etc/app.conf に適用されます", seen.get('label', ""))

    def test_a_link_whose_name_cannot_be_read_still_says_where_it_goes(self):
        """readlink に応じない機器でも、リンク先へ適用されることは伝えること。"""
        m = self._manager(self._targets(), links={})
        by_name = self._listing(m)

        seen = self._chmod(m, by_name["app.conf"])

        self.assertIn("リンク先", seen.get('label', ""))
        self.assertIn("app.conf", seen.get('label', ""))
        self.assertEqual(seen.get('default'), "0600")

    def test_a_changed_mode_on_a_link_is_sent(self):
        """値を変えたときは、これまでどおり送ること。"""
        m = self._manager(self._targets(), {"/base/app.conf": "/etc/app.conf"})
        by_name = self._listing(m)

        self._chmod(m, by_name["app.conf"], answer="0640")

        m.sftp_client.chmod.assert_called_once_with("/base/app.conf", 0o640)

    def test_a_broken_link_is_refused_with_the_reason(self):
        """リンク先を読めなければダイアログを開かず、理由を伝えること。"""
        m = self._manager(self._targets())
        by_name = self._listing(m)

        seen = self._chmod(m, by_name["deadlink"])

        self.assertEqual(seen, {}, "読めないリンク先の権限変更ダイアログが開いた")
        m.sftp_client.chmod.assert_not_called()
        self.assertTrue(self._wait(self.errors), "断った理由が伝わらない")
        self.assertIn("リンク先を読めない", self.errors[0])
        self.assertIn("No such file", self.errors[0])

    def test_a_link_lookup_that_times_out_closes_the_session(self):
        """リンク先の確認が期限切れになったら、他の操作と同じく接続を畳むこと。"""
        targets = self._targets()
        m = self._manager(targets)
        by_name = self._listing(m)
        # 一覧は取れたあと、権限変更の問い合わせで機器が黙る
        targets["/base/app.conf"] = TimeoutError()
        client = m.sftp_client   # 畳まれると None になる

        seen = self._chmod(m, by_name["app.conf"])

        self.assertEqual(seen, {})
        client.chmod.assert_not_called()
        self.assertTrue(self._wait(self.errors), "期限切れが伝わらない")
        self.assertIn("応答しません", self.errors[0])
        self.assertFalse(m.is_connected, "使えなくなったチャンネルを接続中のまま残した")

    def test_a_plain_file_is_not_looked_up_again(self):
        """リンクでない項目は一覧の mode のまま（追加の問い合わせをしない）。"""
        m = self._manager(self._targets())
        by_name = self._listing(m)

        seen = self._chmod(m, by_name["run.cfg"], answer="0600")

        self.assertEqual(seen.get('default'), "0644")
        self.assertNotIn("リンク先", seen.get('label', ""))
        m.sftp_client.stat.assert_not_called()
        m.sftp_client.chmod.assert_called_once_with("/base/run.cfg", 0o600)


if __name__ == "__main__":
    unittest.main()
