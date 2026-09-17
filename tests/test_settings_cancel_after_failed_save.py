"""設定の保存に失敗してキャンセルしたら、変更が残らないことを検証する。

save_settings は共有 ConfigManager のメモリ上の値を書き換えてから
save_config() を呼ぶ。書き込みに失敗すると警告を出して閉じないが、
メモリ上の値はもう書き換わったままで、SFTP パネルはその値をそのまま
読む。利用者がキャンセルしても「削除の前に確認する」が黙って無効に
なり、さらに後続の無関係な保存（レイアウト保存など）が成功した瞬間に、
取り消したはずの値がディスクへ永続化される。

計測: confirm_delete True の状態で外して OK → 警告 1 回、結果は
Accepted でない → キャンセル → config のメモリ上 confirm_delete が
False、パネルの読みも False。ディスクはまだ True だが、無関係な
保存の成功後にディスクも False になった。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SettingsCancelAfterFailedSaveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _manager(self):
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-settingsfail-")
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        cm.set_server_settings("sftp", {"confirm_delete": True})
        cm.set_server_settings("terminal", {"font_size": 11})
        cm.set_check_on_startup(True)
        return cm

    def _on_disk(self, cm):
        with open(cm.config_path, encoding="utf-8") as f:
            return json.load(f)

    def _fail_ok_then_cancel(self, cm, break_save):
        """OK（保存失敗）→ キャンセル、の操作を再現する。"""
        from ui.dialogs.settings_dialog import SettingsDialog
        dlg = SettingsDialog(None, config_manager=cm)
        dlg.confirm_delete_box.setChecked(False)
        dlg.font_size_spin.setValue(13)
        dlg.check_on_startup_box.setChecked(False)
        real_save = cm.save_config
        cm.save_config = break_save
        try:
            with mock.patch("ui.dialogs.settings_dialog.QMessageBox.warning") as warn:
                dlg._on_ok()
        finally:
            cm.save_config = real_save
        warn.assert_called_once()
        self.last_warning = warn.call_args[0][2]
        self.assertNotEqual(dlg.result(), int(dlg.DialogCode.Accepted))
        dlg.reject()
        return dlg

    def test_cancel_after_a_failed_save_leaves_the_settings_untouched(self):
        """保存できなかった変更は、キャンセルしたら効かないこと。"""
        cm = self._manager()
        self._fail_ok_then_cancel(cm, mock.Mock(return_value=False))

        self.assertTrue(cm.get_server_settings("sftp")["confirm_delete"],
                        "取り消したはずの削除確認の無効化が効いている")
        self.assertEqual(cm.get_server_settings("terminal")["font_size"], 11)
        self.assertTrue(cm.get_check_on_startup())

    def test_the_panel_does_not_see_the_cancelled_change(self):
        """SFTP パネルが読む値も元のままであること（削除確認が黙って消えない）。"""
        from ui.sftp_panel import SFTPPanel
        cm = self._manager()
        panel = SFTPPanel(config_manager=cm)
        self._fail_ok_then_cancel(cm, mock.Mock(return_value=False))
        self.assertTrue(panel._get_sftp_setting("confirm_delete", True))

    def test_a_later_unrelated_save_does_not_persist_the_cancelled_change(self):
        """後続の無関係な保存で、取り消した値がディスクへ書かれないこと。"""
        cm = self._manager()
        self._fail_ok_then_cancel(cm, mock.Mock(return_value=False))

        self.assertTrue(cm.set_server_settings("ui_layout", {"tool_tab": 2}))
        disk = self._on_disk(cm)
        self.assertTrue(disk["settings"]["sftp"]["confirm_delete"],
                        "取り消した変更がディスクへ永続化された")
        self.assertEqual(disk["settings"]["terminal"]["font_size"], 11)
        self.assertTrue(disk["update_settings"]["check_on_startup"])

    def test_a_partial_failure_rolls_back_the_sections_that_did_save(self):
        """途中のセクションだけ失敗しても、全体を元へ戻すこと。

        利用者には「保存できませんでした」としか伝わらないので、
        一部だけ効いている状態を残さない。
        """
        cm = self._manager()
        real_save = cm.save_config
        calls = []

        def fail_on_second_call():
            calls.append(1)
            if len(calls) == 2:
                return False
            return real_save()

        self._fail_ok_then_cancel(cm, fail_on_second_call)
        self.assertEqual(cm.get_server_settings("terminal")["font_size"], 11)
        self.assertTrue(cm.get_server_settings("sftp")["confirm_delete"])
        self.assertTrue(cm.get_check_on_startup())
        # メモリだけでは足りない。失敗前のセクションは既にディスクへ書かれて
        # おり、失敗後に成功した setter が変更後のメモリを config 丸ごと
        # 書き出すので、ディスクには取り消した変更が全部残る。
        disk = self._on_disk(cm)
        self.assertEqual(disk["settings"]["terminal"]["font_size"], 11,
                         "失敗前に書けたセクションがディスクに残っている")
        self.assertTrue(disk["settings"]["sftp"]["confirm_delete"])
        self.assertTrue(disk["update_settings"]["check_on_startup"],
                        "失敗後に成功した setter が取り消した値を書き出している")
        # 次の起動（＝再読み込み）でも戻っていること
        from core.config_manager import ConfigManager
        reloaded = ConfigManager(config_path=str(cm.config_path))
        self.assertTrue(reloaded.get_server_settings("sftp")["confirm_delete"],
                        "再起動で削除確認が黙って無効に戻っている")
        self.assertEqual(reloaded.get_server_settings("terminal")["font_size"], 11)
        self.assertTrue(reloaded.get_check_on_startup())
        self.assertNotIn("元に戻す", self.last_warning,
                         "ディスクも戻せたのに戻せなかったと伝えている")

    def test_the_warning_says_when_even_the_rollback_could_not_be_saved(self):
        """戻した内容もディスクへ書けなかったときは、そう伝えること。"""
        cm = self._manager()
        self._fail_ok_then_cancel(cm, mock.Mock(return_value=False))
        self.assertIn("元に戻す", self.last_warning)

    def test_a_successful_save_still_applies_everything(self):
        """保存できたときは、これまでどおり全部効くこと。"""
        from ui.dialogs.settings_dialog import SettingsDialog
        cm = self._manager()
        dlg = SettingsDialog(None, config_manager=cm)
        dlg.confirm_delete_box.setChecked(False)
        dlg.font_size_spin.setValue(13)
        dlg._on_ok()
        self.assertEqual(dlg.result(), int(dlg.DialogCode.Accepted))
        self.assertFalse(cm.get_server_settings("sftp")["confirm_delete"])
        self.assertEqual(cm.get_server_settings("terminal")["font_size"], 13)
        disk = self._on_disk(cm)
        self.assertFalse(disk["settings"]["sftp"]["confirm_delete"])


if __name__ == "__main__":
    unittest.main()
