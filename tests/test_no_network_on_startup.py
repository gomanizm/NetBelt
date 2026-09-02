"""テストを回すだけで外部へ出ていかないことを検証する。

MainWindow() を組むと _check_for_updates_on_startup が走る。これは
config.json の update_settings.check_on_startup（既定 True）だけを見て、
バックグラウンドスレッドで api.github.com へ問い合わせる。トークンが
設定されていれば（環境変数 GITHUB_TOKEN か config.json）Authorization
ヘッダに載せて送る。

テストは MainWindow を何度も組むので、pytest を1回回すだけで外部への
HTTPS が何度も飛び、開発者や CI のトークンが GitHub へ渡る。オフライン
やレート制限では不安定になり、%TEMP% に未適用の更新 ZIP が残っている
端末では、_check_pending_updates のモーダルでテストが止まる。

個々のテストが自分でパッチする形だと、書き忘れたファイルから漏れる
（実際、複数のファイルで抜けていた）。conftest で一律に止める。
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class NoNetworkOnStartupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_building_the_window_does_not_reach_the_network(self):
        """MainWindow を組んだだけで外部へ要求を出さないこと。"""
        import core.version_manager as version_manager
        from ui.main_window import MainWindow

        calls = []

        def spy(url, *args, **kwargs):
            calls.append(url)
            raise IOError("テストからは外へ出さない")

        with mock.patch.object(version_manager.requests, "get", spy):
            MainWindow()
            # 問い合わせはバックグラウンドスレッドなので、少し回して待つ
            deadline = time.time() + 2.0
            while time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.05)

        self.assertEqual(calls, [],
                         "テストを回すだけで外部へ出ている: %s" % calls)


if __name__ == "__main__":
    unittest.main()
