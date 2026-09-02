"""属性の一部を返さない SFTP サーバでも、一覧描画が落ちないことを検証する。

paramiko の SFTPAttributes は、サーバが ATTR_ACMODTIME / ATTR_SIZE の
フラグを立てなかった項目について st_mtime / st_size を None のままにする。
SFTPManager はその None をそのまま dict に載せるので、UI 側が無防備に
datetime.fromtimestamp() や書式化へ渡すと TypeError になる。

これは file_list_ready のスロット、つまりバックグラウンドスレッドからの
キュー接続で呼ばれるため、例外は Qt のイベントループ内で未処理になる。
PyQt6 はその場合プロセスごと落とすので、SFTP パネルのエラーでは済まず、
他のターミナルタブや起動中の TFTP/FTP/Syslog サーバまで巻き添えになる。

値が無いときは 0 や 1970-01-01 に丸めない。丸めると「サイズ 0 のファイル」
「1970年に更新されたファイル」という別の嘘になり、転送の判断を誤らせる。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class SftpPanelMissingAttrsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self):
        from ui.sftp_panel import SFTPPanel
        return SFTPPanel()

    @staticmethod
    def _entry(**overrides):
        """listdir_attr 由来の1項目。既定は属性が揃っている状態。"""
        entry = {
            'name': 'running-config',
            'size': 4096,
            'mtime': 1700000000,
            'mode': 0o100644,
            'is_dir': False,
            'permissions': '-rw-r--r--',
        }
        entry.update(overrides)
        return entry

    def _row_text(self, panel, column):
        item = panel.model.item(0, column)
        return item.text() if item is not None else None

    def test_an_entry_without_mtime_does_not_raise(self):
        """更新日時を返さない項目があっても描画が完走すること。"""
        panel = self._panel()
        panel._update_file_list([self._entry(mtime=None)])
        self.assertEqual(panel.model.rowCount(), 1,
                         "更新日時が無い項目で行が作られていない")

    def test_an_entry_without_size_does_not_raise(self):
        """サイズを返さない項目があっても描画が完走すること。"""
        panel = self._panel()
        panel._update_file_list([self._entry(size=None)])
        self.assertEqual(panel.model.rowCount(), 1,
                         "サイズが無い項目で行が作られていない")

    def test_an_unknown_mtime_is_not_shown_as_1970(self):
        """更新日時が不明な項目を 1970-01-01 と表示しないこと。

        0 に丸めるとエポックの日付が出る。実際に 1970 年のファイルと
        区別がつかなくなるので、不明は不明と出す。
        """
        panel = self._panel()
        panel._update_file_list([self._entry(mtime=None)])
        shown = self._row_text(panel, 3)
        self.assertNotIn("1970", shown or "",
                         "不明な更新日時がエポックの日付として出ている")

    def test_an_unknown_size_is_not_shown_as_zero_bytes(self):
        """サイズが不明な項目を 0.0 B と表示しないこと。"""
        panel = self._panel()
        panel._update_file_list([self._entry(size=None)])
        shown = self._row_text(panel, 1)
        self.assertNotIn("0.0", shown or "",
                         "不明なサイズが 0 バイトとして出ている")

    def test_a_normal_entry_still_shows_its_size_and_time(self):
        """属性が揃っている項目の表示は変えないこと。"""
        panel = self._panel()
        panel._update_file_list([self._entry()])
        self.assertIn("4.0 KB", self._row_text(panel, 1) or "",
                      "通常の項目のサイズ表示が変わっている")
        self.assertIn("2023", self._row_text(panel, 3) or "",
                      "通常の項目の更新日時が出ていない")

    def test_a_negative_mtime_does_not_raise(self):
        """更新日時が負の値でも描画が完走すること。

        paramiko は SFTP v3 の ATTR_ACMODTIME を符号付き 32bit として読む
        （sftp_attr.py の _unpack が msg.get_int() を使う）ので、機器が
        大きな値を入れると負に化ける。Windows の fromtimestamp() は負の
        タイムスタンプで OSError を投げるため、None と同じ経路で落ちる。
        """
        panel = self._panel()
        panel._update_file_list([self._entry(mtime=-2000000000)])
        self.assertEqual(panel.model.rowCount(), 1,
                         "負の更新日時で行が作られていない")

    def test_a_broken_entry_does_not_hide_the_rest(self):
        """壊れた項目が1つあっても、他の項目は表示すること。"""
        panel = self._panel()
        panel._update_file_list([
            self._entry(name='a-good', size=100, mtime=1700000000),
            self._entry(name='b-broken', size=None, mtime=None),
            self._entry(name='c-good', size=200, mtime=1700000000),
        ])
        self.assertEqual(panel.model.rowCount(), 3,
                         "壊れた項目のせいで他の項目まで消えている")


if __name__ == "__main__":
    unittest.main()
