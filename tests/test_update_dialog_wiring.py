"""更新ダイアログが VersionManager へ正しく引数を渡すことを確認する。

VersionManager 単体のテストだけでは、UI 側の配線ミス（引数の受け渡し漏れ）を
見逃す。実際、DownloadThread が sha256_url を受け取らないまま呼び出し側だけ
渡している状態を作ってしまい、単体テストは全て通っていた。
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, "src")


class UpdateDialogWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_download_thread_accepts_sha256_url(self):
        """呼び出し側が渡すキーワードを、コンストラクタが受け取れること。"""
        from ui.dialogs.update_dialog import DownloadThread
        t = DownloadThread("https://example.com/a.zip", None, None,
                           sha256_url="https://example.com/a.zip.sha256")
        self.assertEqual(t.sha256_url, "https://example.com/a.zip.sha256")

    def test_download_thread_defaults_to_none(self):
        from ui.dialogs.update_dialog import DownloadThread
        t = DownloadThread("https://example.com/a.zip")
        self.assertIsNone(t.sha256_url)

    def test_run_passes_sha256_url_to_version_manager(self):
        """run() が download_update へ sha256_url を渡すこと。"""
        from ui.dialogs.update_dialog import DownloadThread
        t = DownloadThread("https://example.com/a.zip", None, None,
                           sha256_url="https://example.com/a.zip.sha256")
        with unittest.mock.patch.object(t.version_mgr, "download_update",
                                        return_value="C:/tmp/a.zip") as m:
            t.run()
        m.assert_called_once()
        self.assertEqual(m.call_args.kwargs.get("sha256_url"),
                         "https://example.com/a.zip.sha256")

    # 「呼び出し側のキーワードが定義側に存在するか」の一般検査は
    # tests/test_call_signatures.py が AST で全ソースに対して行う。
    # ここに文字列検索で書いていた版は、呼び出し側を消しても緑のままで
    # 検出力が無かったため取り下げた。

if __name__ == "__main__":
    unittest.main()
