"""他アプリの config.json で起動した回の画面の文と、設定ファイルのパスの表示を検証する。

何が起きていたか（f6aca19 で実測。1.3.4 の最終検査の SCU-1・SCU-2・CB-1・DR-1）:
  - バージョン情報の「設定ファイル:」は、html.escape したパスを RichText に
    入れていた。連続した空白が 1 つに縮み、表示のとおりに打ち込んでも
    見つからないパスになっていた。
  - 起動したフォルダの config.json が他アプリのもの（foreign_config_path が
    立つ）回は、save_config が必ず失敗する。機器・グループ・プリセットの
    追加・変更・削除と設定画面の OK は、メモリも元に戻して断られる（ファイルは
    バイト単位で変わらない）。表示メニューのフォントサイズ拡大など、ほかの設定はメモリに
    だけ効く。ところが起動時の知らせは「変更した設定は保存されません。」と
    だけ書いていた。機器の追加の失敗は理由を出さず、プリセットの保存は
    「書き込めるか確認してください」と出していた。設定画面の OK は「元に戻す
    こともできませんでした。書き込めない可能性があります」と、違う理由を
    出していた。バージョン情報は、使っていない他アプリのファイルを、注記なしで
    出していた。

直し方: パスは white-space: pre-wrap の span で包む（&nbsp; にはしない。
コピーしても本物の空白のまま残るように）。他アプリの config.json の回だけ、
起動時の知らせと同じ理由（FOREIGN_CONFIG_REFUSAL）を各画面の文に出し、
バージョン情報のパスに、使っていないことを添える。そうでない回（読み取り
専用の config.json など）の文と経路は変えない。
"""
import contextlib
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

# 他アプリの dict（test_config_foreign_json_left_alone.py と同じ形）
FOREIGN_DICT = (b'{\n'
                b'  "other_application_setting": "keep-me",\n'
                b'  "settings": {"ftp_server": {"password": "plain-secret"}}\n'
                b'}\n')

# 読み取り専用にする NetBelt の設定（機器 1 台・プリセット 1 件）
NETBELT_CONFIG = {
    "config_version": "1.0",
    "groups": [
        {"name": "Default", "auto_commands": [], "devices": []},
        {"name": "Lab", "auto_commands": [], "devices": [
            {"name": "rtr1", "host": "192.0.2.1", "port": 22,
             "protocol": "ssh", "username": "u", "password": "",
             "ssh_key": "", "macros": []}]},
    ],
    "global_macros": [{"name": "check", "commands": ["show version"]}],
    "settings": {},
    "update_settings": {"check_on_startup": False},
}

# 連続した空白（2 つと 3 つ）を含むフォルダ名
SPACED_PREFIX = "netbelt-a  b   c-"


def _new_device(name):
    return {"name": name, "host": "192.0.2.10", "port": 22, "protocol": "ssh",
            "username": "u", "password": "", "ssh_key": "", "macros": []}


class _WindowTestCase(unittest.TestCase):
    _windows = []   # 配送待ちシグナルの宛先を先に解放しない（他テストと同じ理由）

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        cls._windows.clear()

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix=SPACED_PREFIX)
        self.path = os.path.join(self.dir, "config.json")
        home = mock.patch(
            "core.config_manager.app_data_dir",
            return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)

    def _window(self):
        """作業ディレクトリを self.dir へ移し、相対の config.json で窓を作る"""
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        prev = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, prev)
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
                mock.patch("PyQt6.QtWidgets.QMessageBox.warning") as warning:
            fake.return_value = ConfigManager("config.json")
            window = MainWindow()
        self._windows.append(window)
        return window, warning

    def _on_disk(self):
        with open(self.path, "rb") as f:
            return f.read()

    # --- 画面の操作（警告の文を返す） ---

    @staticmethod
    def _device_dialog(data, group):
        from PyQt6.QtWidgets import QDialog
        dialog = mock.Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_device_data.return_value = data
        dialog.get_selected_group.return_value = group
        dialog.group_combo.findText.return_value = 0
        return dialog

    def _warning_of(self, window, action, dialog=None, target="DeviceDialog"):
        """action を実行し、出た警告（1 回だけ）の文を返す。確認には「はい」"""
        from PyQt6.QtWidgets import QMessageBox
        with contextlib.ExitStack() as stack:
            warn = stack.enter_context(
                mock.patch("ui.main_window.QMessageBox.warning"))
            stack.enter_context(mock.patch(
                "ui.main_window.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes))
            if dialog is not None:
                stack.enter_context(mock.patch("ui.main_window." + target,
                                               return_value=dialog))
            action()
        self.assertEqual(warn.call_count, 1, warn.call_args_list)
        return warn.call_args[0][2]

    def _device_warnings(self, window):
        """機器の追加・複製・編集・削除・移動の失敗の文（操作名 -> 文）"""
        cm = window.config_manager
        group, device = next((g["name"], d) for g in cm.get_groups()
                             for d in g.get("devices", []))
        other = next(g["name"] for g in cm.get_groups() if g["name"] != group)
        shown = {
            "add": self._warning_of(
                window, window._on_add_device,
                self._device_dialog(_new_device("added"), other)),
            "duplicate": self._warning_of(
                window, lambda: window._on_device_duplicate(group, device),
                self._device_dialog(_new_device("copied"), group)),
            "edit": self._warning_of(
                window, lambda: window._on_device_edit(group, device),
                self._device_dialog(dict(device, host="192.0.2.99"), group)),
            "delete": self._warning_of(
                window, lambda: window._on_device_delete(
                    group, device["name"], device)),
            "move": self._warning_of(
                window, lambda: window._on_device_moved(
                    group, other, device["name"], device)),
        }
        # どれも断られ、メモリも変わっていない
        names = [d.get("name") for g in cm.get_groups()
                 for d in g.get("devices", [])]
        self.assertNotIn("added", names)
        self.assertNotIn("copied", names)
        self.assertEqual(cm.get_group(group)["devices"][0], device)
        return shown

    def _group_warnings(self, window):
        """グループの追加・改名の失敗の文（操作名 -> 文）"""
        from PyQt6.QtWidgets import QDialog
        cm = window.config_manager
        existing = cm.get_groups()[-1]["name"]
        dialog = mock.Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_auto_commands.return_value = []
        dialog.get_group_name.return_value = "added-group"
        added = self._warning_of(window, window._on_add_group, dialog,
                                 target="GroupDialog")
        dialog.get_group_name.return_value = "renamed-group"
        renamed = self._warning_of(
            window, lambda: window._on_edit_group(existing), dialog,
            target="GroupDialog")
        self.assertIsNone(cm.get_group("added-group"))
        self.assertIsNone(cm.get_group("renamed-group"))
        self.assertIsNotNone(cm.get_group(existing))
        return {"add": added, "rename": renamed}

    def _settings_ok_warning(self, window):
        """設定画面でフォントを変えて OK を押したときの文"""
        from ui.dialogs import settings_dialog
        cm = window.config_manager
        before = cm.get_server_settings("terminal").get("font_size")
        dialog = settings_dialog.SettingsDialog(None, config_manager=cm)
        self.addCleanup(dialog.deleteLater)
        dialog.font_size_spin.setValue(dialog.font_size_spin.value() + 3)
        with mock.patch.object(settings_dialog.QMessageBox, "warning") as warn, \
                mock.patch.object(dialog, "accept") as accept:
            dialog._on_ok()
        self.assertEqual(warn.call_count, 1, warn.call_args_list)
        accept.assert_not_called()
        self.assertEqual(cm.get_server_settings("terminal").get("font_size"),
                         before, "断った変更がメモリに残った")
        return warn.call_args[0][2]

    def _preset_warnings(self, window):
        """プリセットの新規保存・更新・削除の失敗の文（操作名 -> 文）"""
        from PyQt6.QtWidgets import QMessageBox
        from ui.dialogs import macro_dialog
        cm = window.config_manager
        existing = cm.get_global_macros()[0]
        shown = {}
        for key, name in (("new", ""), ("update", existing["name"])):
            dialog = macro_dialog.PresetEditDialog(
                None, config_manager=cm, preset_name=name,
                commands=list(existing["commands"]))
            self.addCleanup(dialog.deleteLater)
            dialog.name_edit.setText(name or "new-preset")
            dialog.command_text.setPlainText("show clock")
            with mock.patch.object(macro_dialog.QMessageBox, "warning") as warn, \
                    mock.patch.object(macro_dialog.QMessageBox, "information"):
                dialog._on_save()
            self.assertEqual(warn.call_count, 1, warn.call_args_list)
            shown[key] = warn.call_args[0][2]
        dialog = macro_dialog.MacroDialog(None, device_name="R1",
                                          config_manager=cm)
        self.addCleanup(dialog.deleteLater)
        dialog.preset_list_widget.setCurrentRow(0)
        with mock.patch.object(macro_dialog.QMessageBox, "warning") as warn, \
                mock.patch.object(macro_dialog.QMessageBox, "question",
                                  return_value=QMessageBox.StandardButton.Yes), \
                mock.patch.object(macro_dialog.QMessageBox, "information"):
            dialog._on_preset_delete()
        self.assertEqual(warn.call_count, 1, warn.call_args_list)
        shown["delete"] = warn.call_args[0][2]
        self.assertEqual(cm.get_global_macros(), [existing],
                         "断ったプリセットの変更がメモリに残った")
        return shown

    def _shown_config_path(self, window):
        """バージョン情報の QLabel の文を平文にした「設定ファイル:」の行の残り

        toPlainText は U+00A0 を空白に置き換えるので、&nbsp; での表示と
        見分けがつかない。toRawText で読み、段落と改行の区切りで分ける。
        """
        from PyQt6.QtGui import QTextDocument
        from PyQt6.QtWidgets import QLabel
        shown = []

        def capture(dialog):
            label = dialog.findChild(QLabel, "qt_msgbox_label")
            shown.append(label.text() if label is not None else dialog.text())
            return 0

        with mock.patch.object(window, "_exec_dialog", side_effect=capture):
            window._on_version_info()
        self.assertEqual(len(shown), 1)
        document = QTextDocument()
        document.setHtml(shown[0])
        head = "設定ファイル: "
        for line in re.split("[\n\u2028\u2029]", document.toRawText()):
            if line.startswith(head):
                return line[len(head):]
        self.fail("「設定ファイル:」の行が無い: %r" % document.toRawText())


class VersionInfoShowsThePathAsIsTest(_WindowTestCase):
    """バージョン情報の設定ファイルのパスが、連続した空白も含めて実物と同じ"""

    def test_a_path_with_runs_of_spaces_is_shown_as_is(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(NETBELT_CONFIG, f)
        window, warning = self._window()
        self.assertEqual(warning.call_count, 0, warning.call_args_list)

        shown = self._shown_config_path(window)

        self.assertIn("  ", self.path, "前提: パスに連続した空白がある")
        self.assertEqual(shown, self.path)
        self.assertTrue(os.path.exists(shown), "表示どおりのパスに無い")
        self.assertNotIn("読み書きしていません", shown)

    def test_a_foreign_config_path_says_it_is_not_used(self):
        from core.config_manager import FOREIGN_CONFIG_PATH_NOTE
        with open(self.path, "wb") as f:
            f.write(FOREIGN_DICT)
        window, _ = self._window()

        shown = self._shown_config_path(window)

        self.assertEqual(shown, self.path + FOREIGN_CONFIG_PATH_NOTE)
        self.assertIn("NetBelt の設定ではないため、読み書きしていません", shown)
        self.assertIn("今回は既定の設定で動いています", shown)


class ForeignConfigMessagesTest(_WindowTestCase):
    """他アプリの config.json の回は、どの画面も起動時の知らせと同じ理由を出す"""

    def setUp(self):
        super().setUp()
        with open(self.path, "wb") as f:
            f.write(FOREIGN_DICT)
        self.window, self.startup_warning = self._window()
        self.assertTrue(self.window.config_manager.foreign_config_path,
                        "前提: 他アプリの config.json として断っている")

    def tearDown(self):
        self.assertEqual(self._on_disk(), FOREIGN_DICT,
                         "他アプリの config.json が変わった")

    def _reason(self):
        from core.config_manager import FOREIGN_CONFIG_REFUSAL
        self.assertIn("機器・グループ・プリセットの追加・変更・削除",
                      FOREIGN_CONFIG_REFUSAL)
        self.assertIn("受け付けません", FOREIGN_CONFIG_REFUSAL)
        # 設定画面の OK を断る訳も、この文が言う（各画面の文は定数そのものと
        # 照らすので、ここで見ないと節が落ちても気づけない）。括弧の中の
        # メニューの道筋は呼び方を直しても落ちないよう、照らさない
        self.assertRegex(FOREIGN_CONFIG_REFUSAL,
                         "設定画面.*の変更は受け付けません")
        return FOREIGN_CONFIG_REFUSAL

    def test_the_startup_notice_says_what_is_refused(self):
        self.assertEqual(self.startup_warning.call_count, 1,
                         self.startup_warning.call_args_list)
        text = self.startup_warning.call_args[0][2]

        self.assertIn(self.path, text)
        self.assertIn(self._reason(), text)
        self.assertIn("表示メニューのフォントサイズ拡大・縮小（Ctrl+ホイール）"
                      "など、そのほかの設定の変更はこの回だけ効き、"
                      "保存されません。", text)
        self.assertNotIn("変更した設定は保存されません", text)

    def test_other_settings_take_effect_only_for_this_run(self):
        """起動時の知らせのとおり、表示メニューのフォントサイズ拡大はメモリにだけ効く"""
        cm = self.window.config_manager
        before = cm.get_server_settings("terminal").get("font_size")

        self.window._change_font_size(1)

        self.assertEqual(cm.get_server_settings("terminal").get("font_size"),
                         before + 1)

    def test_device_changes_give_the_reason(self):
        shown = self._device_warnings(self.window)

        reason = "\n\n" + self._reason()
        self.assertEqual(shown, {
            "add": "機器の追加に失敗しました。" + reason,
            "duplicate": "機器の追加に失敗しました。" + reason,
            "edit": "機器の更新に失敗しました。設定は変更されていません。" + reason,
            "delete": "機器の削除に失敗しました。" + reason,
            "move": "機器の移動に失敗しました。" + reason,
        })

    def test_group_changes_give_the_reason(self):
        shown = self._group_warnings(self.window)

        tail = "保存できなかったので、変更は反映していません。\n\n" + self._reason()
        self.assertEqual(shown, {
            "add": "グループの追加を設定ファイルへ保存できませんでした。\n" + tail,
            "rename": "グループ名の変更を設定ファイルへ保存できませんでした。\n"
                      + tail,
        })

    def test_settings_ok_gives_the_reason_not_a_write_error(self):
        text = self._settings_ok_warning(self.window)

        self.assertEqual(text, "設定の変更は反映していません。\n\n" + self._reason())
        self.assertNotIn("元に戻すこともできません", text)
        self.assertNotIn("書き込めない", text)

    def test_preset_saves_give_the_reason_not_a_write_check(self):
        shown = self._preset_warnings(self.window)

        reason = "\n\n" + self._reason()
        self.assertEqual(shown, {
            "new": "プリセット 'new-preset' を保存できませんでした。" + reason,
            "update": "プリセットの更新に失敗しました。" + reason,
            "delete": "プリセットの削除に失敗しました。" + reason,
        })
        self.assertNotIn("書き込めるか", shown["new"])


class ReadOnlyConfigMessagesUnchangedTest(_WindowTestCase):
    """他アプリの config.json でない回（読み取り専用）の文は今までどおり"""

    def setUp(self):
        super().setUp()
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(NETBELT_CONFIG, f)
        os.chmod(self.path, stat.S_IREAD)
        self.addCleanup(os.chmod, self.path, stat.S_IREAD | stat.S_IWRITE)
        self.before = self._on_disk()
        self.window, warning = self._window()
        self.assertEqual(warning.call_count, 0, warning.call_args_list)
        self.assertIsNone(self.window.config_manager.foreign_config_path)

    def tearDown(self):
        self.assertEqual(self._on_disk(), self.before)

    def test_device_messages_are_unchanged(self):
        self.assertEqual(self._device_warnings(self.window), {
            "add": "機器の追加に失敗しました。",
            "duplicate": "機器の追加に失敗しました。",
            "edit": "機器の更新に失敗しました。設定は変更されていません。",
            "delete": "機器の削除に失敗しました。",
            "move": "機器の移動に失敗しました。",
        })

    def test_group_messages_are_unchanged(self):
        tail = "保存できなかったので、変更は反映していません。"
        self.assertEqual(self._group_warnings(self.window), {
            "add": "グループの追加を設定ファイルへ保存できませんでした。\n" + tail,
            "rename": "グループ名の変更を設定ファイルへ保存できませんでした。\n"
                      + tail,
        })

    def test_settings_ok_message_is_unchanged(self):
        self.assertEqual(self._settings_ok_warning(self.window),
                         "設定を保存できず、元に戻すこともできませんでした。\n"
                         "設定ファイルに書き込めない可能性があります。")

    def test_preset_messages_are_unchanged(self):
        self.assertEqual(self._preset_warnings(self.window), {
            "new": "プリセット 'new-preset' を保存できませんでした。\n"
                   "設定ファイル (config.json) に書き込めるか確認してください。",
            "update": "プリセットの更新に失敗しました。",
            "delete": "プリセットの削除に失敗しました。",
        })


if __name__ == "__main__":
    unittest.main()
