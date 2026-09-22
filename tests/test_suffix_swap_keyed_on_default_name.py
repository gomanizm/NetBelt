"""拡張子の付け替えを、拡張子ではなく既定のファイル名で決めることを検証する。

何が起きていたか（実測、基準 63c27f5）: apply_filter_suffix は
「既定の拡張子と同じなら付け替える」で判定していた
（core/save_defaults.py の `ext.lower() == default_ext.lower()`）。
既定の名前は snmp_result_20260923_101500.txt のように必ず .txt なので、
利用者が名前を out.txt と打ち直しても拡張子は既定と同じままであり、
「名前を変えずに種類だけ選んだ」場合と区別が付かない。そのため
out.txt と入れて絞り込みで CSV を選ぶと out.csv へ付け替えられ、
利用者が名指ししたのとは別の名前のファイルが保存先になっていた。

どう直したか（検査役の案）: 既定のファイル名そのものと一致するとき
（＝名前に触らず種類だけ選んだとき）に付け替える。

    if default_name and os.path.basename(file_path) == default_name:
        return root + allowed[0]
    return file_path

拡張子が無いときに選んだ種類のものを足す振る舞いは変えない（out と
だけ打った場合は、どの種類でも拡張子が要る）。
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

TABLE_FILTERS = "テキスト (*.txt);;CSV (*.csv);;JSON (*.json)"
CSV_FILTER = "CSV (*.csv)"
DEFAULT_NAME = "snmp_result_20260923_101500.txt"


class SuffixSwapKeyedOnDefaultNameTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-swapkey-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    # --- 判定そのもの ----------------------------------------------------

    def test_a_renamed_file_keeps_its_name_even_with_the_default_extension(self):
        """名前を打ち直したら、拡張子が既定と同じでも付け替えないこと。"""
        from core import save_defaults
        self.assertEqual(
            save_defaults.apply_filter_suffix(
                os.path.join(self.dir, "out.txt"), CSV_FILTER, DEFAULT_NAME),
            os.path.join(self.dir, "out.txt"))

    def test_the_default_name_still_follows_the_chosen_type(self):
        """名前に触らず種類だけ選んだときは、これまでどおり付け替えること。"""
        from core import save_defaults
        self.assertEqual(
            save_defaults.apply_filter_suffix(
                os.path.join(self.dir, DEFAULT_NAME), CSV_FILTER,
                DEFAULT_NAME),
            os.path.join(self.dir, os.path.splitext(DEFAULT_NAME)[0]
                         + ".csv"))

    def test_a_name_without_an_extension_still_gets_one(self):
        """拡張子を書かなかったときは、名前を変えていても足すこと。"""
        from core import save_defaults
        self.assertEqual(
            save_defaults.apply_filter_suffix(
                os.path.join(self.dir, "out"), CSV_FILTER, DEFAULT_NAME),
            os.path.join(self.dir, "out.csv"))

    def test_a_matching_extension_is_left_alone(self):
        """選んだ種類と同じ拡張子なら、名前にも中身にも触らないこと。"""
        from core import save_defaults
        self.assertEqual(
            save_defaults.apply_filter_suffix(
                os.path.join(self.dir, "out.csv"), CSV_FILTER, DEFAULT_NAME),
            os.path.join(self.dir, "out.csv"))

    # --- 画面から通したとき ----------------------------------------------

    def test_snmp_export_writes_the_name_the_user_typed(self):
        """打った名前のまま保存され、中身はその拡張子どおりになること。"""
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-swapkey-conf-")
        self.addCleanup(shutil.rmtree, d, True)
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = cm
            window = MainWindow()
        type(self)._windows.append(window)
        panel = window.snmp_panel
        panel.result_model.set_results(
            [("1.3.6.1.2.1.1.5.0", "OctetString", "sw1")])
        panel._result_host = "192.0.2.10"

        def fake_dialog(parent, title, initial, filters="", *args, **kwargs):
            return os.path.join(self.dir, "out.txt"), CSV_FILTER

        with mock.patch("ui.snmp_panel.QFileDialog.getSaveFileName",
                        side_effect=fake_dialog), \
                mock.patch("ui.snmp_panel.QMessageBox"):
            panel._on_export_clicked()

        names = [n for n in os.listdir(self.dir) if not n.endswith(".tmp")]
        self.assertEqual(names, ["out.txt"],
                         "打った名前と違うファイルが作られた: %r" % names)
        with io.open(os.path.join(self.dir, "out.txt"),
                     encoding="utf-8-sig") as f:
            body = f.read()
        self.assertNotIn("OID,Type,Value", body,
                         "拡張子は .txt なのに CSV が書かれている")


if __name__ == "__main__":
    unittest.main()
