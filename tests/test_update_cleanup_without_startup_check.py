# -*- coding: utf-8 -*-
"""「起動時に更新を確認する」を切っていても、更新フォルダの掃除と未適用の更新の提示が走ることを検証する。

何が起きていたか。古い更新ファイルの掃除（cleanup_old_updates）と未適用の
更新の提示（_check_pending_updates）は、起動時の更新確認の中でしか呼ばれず、
設定で起動時の確認を切ると先頭で return して一度も走らなかった。手動で
更新するたびに %TEMP%\\NetBeltUpdates へ ZIP（約 50 MB）と控え、中断で残った
.part が積もり続け、落としただけで適用しなかった更新も次の起動で提示されなかった。

確認（GitHub への問い合わせ）は今までどおり設定に従う。
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
    calls = []
    done = threading.Event()

    def __init__(self, github_token=None):
        pass

    def cleanup_old_updates(self, hours):
        self.calls.append(("cleanup", hours))
        self.done.set()

    def check_for_updates(self):
        self.calls.append("check")
        self.done.set()
        return {"available": False}


class CleanupWithoutStartupCheckTest(unittest.TestCase):

    def _run(self, check_on_startup):
        _FakeVersionManager.calls = []
        _FakeVersionManager.done = threading.Event()
        calls = _FakeVersionManager.calls
        window = types.SimpleNamespace(
            config_manager=mock.Mock(**{
                "get_check_on_startup.return_value": check_on_startup,
                "get_github_token.return_value": None,
                "get_skipped_version.return_value": None,
            }),
            _check_pending_updates=lambda: calls.append("pending"),
            update_available=mock.Mock(),
        )
        threads_before = set(threading.enumerate())
        with mock.patch("ui.main_window.VersionManager", _FakeVersionManager):
            _REAL_STARTUP_CHECK(window)
            for t in set(threading.enumerate()) - threads_before:
                t.join(5)
        return calls

    def test_cleanup_and_pending_run_when_startup_check_is_off(self):
        calls = self._run(check_on_startup=False)
        self.assertIn("pending", calls)
        self.assertIn(("cleanup", 24), calls)
        self.assertNotIn("check", calls, "確認は設定どおり行わない")

    def test_startup_check_on_is_unchanged(self):
        calls = self._run(check_on_startup=True)
        self.assertEqual(calls, ["pending", ("cleanup", 24), "check"])


if __name__ == "__main__":
    unittest.main()
