# -*- coding: utf-8 -*-
"""「起動時に更新を確認する」を切っているとき、掃除のためだけに GitHub トークンを読まないことを検証する。

何が起きていたか。設定に関係なく古い更新ファイルを掃除するようにしたあと、
起動のたびに get_github_token() を呼ぶようになった。別の PC で保存された
トークンがあると、GitHub へは問い合わせないのに「GitHubトークンを復号
できませんでした」がログに毎回出ていた。掃除にトークンは要らない。
"""
import sys
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, "src")

# conftest の autouse フィクスチャがテスト中は差し替えるので、収集の時点
# （フィクスチャより前）で本物を控えておく
from ui.main_window import MainWindow as _MainWindow  # noqa: E402

_REAL_STARTUP_CHECK = _MainWindow._check_for_updates_on_startup


class _FakeVersionManager:
    tokens = []
    calls = []

    def __init__(self, github_token=None):
        self.tokens.append(github_token)

    def cleanup_old_updates(self, hours):
        self.calls.append(("cleanup", hours))

    def check_for_updates(self):
        self.calls.append("check")
        return {"available": False}


class CleanupSkipsTokenTest(unittest.TestCase):

    def _run(self, check_on_startup):
        _FakeVersionManager.tokens = []
        _FakeVersionManager.calls = []
        config = mock.Mock(**{
            "get_check_on_startup.return_value": check_on_startup,
            "get_github_token.return_value": "token-for-test",
            "get_skipped_version.return_value": None,
        })
        window = types.SimpleNamespace(
            config_manager=config,
            _check_pending_updates=lambda: None,
            update_available=mock.Mock(),
        )
        threads_before = set(threading.enumerate())
        with mock.patch("ui.main_window.VersionManager", _FakeVersionManager):
            _REAL_STARTUP_CHECK(window)
            for t in set(threading.enumerate()) - threads_before:
                t.join(5)
        return config

    def test_token_not_read_when_startup_check_is_off(self):
        config = self._run(check_on_startup=False)
        self.assertIn(("cleanup", 24), _FakeVersionManager.calls)
        config.get_github_token.assert_not_called()
        self.assertNotIn("token-for-test", _FakeVersionManager.tokens)

    def test_token_used_for_the_check_when_on(self):
        config = self._run(check_on_startup=True)
        config.get_github_token.assert_called_once_with()
        self.assertEqual(_FakeVersionManager.calls, [("cleanup", 24), "check"])
        self.assertIn("token-for-test", _FakeVersionManager.tokens)


if __name__ == "__main__":
    unittest.main()
