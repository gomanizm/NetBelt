"""GitHub トークンが例外の文字列に乗って外へ出ないことを確認する。

実測（release #1）:

    [VersionManager] 更新チェックエラー: Invalid leading whitespace,
    reserved character(s), or return character(s) in header value:
    'token ghp_SECRETVALUE\\nX-Evil: 1'

改行を含むトークンを設定すると、requests のヘッダ検証が
ヘッダ値そのもの（= `token <トークン>`）を例外文へ埋める。
version_manager はその例外を `print` と `error` の両方へ素通しするため、
凍結ビルドでは %LOCALAPPDATA%\\NetBelt\\logs\\ へ恒久的に残り、
更新ダイアログにも平文で出る。download_update 側には
「トークンの値そのものはログへ出さない」と明示の防御があるのに、
except がそれを破っていた。
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
import unittest.mock

import requests

sys.path.insert(0, "src")

# 実在しない見本。RFC 5737 / example.com と同じ扱いの架空値。
SECRET = "ghp_0000EXAMPLESECRET0000"
NEWLINE_TOKEN = SECRET + "\nX-Injected: 1"


def _headers_only_get(recorded):
    """通信せずに requests と同じヘッダ検証だけを行う差し替え。

    ヘッダが正しければ接続エラーを返す。壊れていれば requests 本体と
    同じ例外（＝ヘッダ値を含む文面）が出る。
    """
    def _get(url, headers=None, **kwargs):
        recorded.append(headers or {})
        requests.models.PreparedRequest().prepare_headers(headers or {})
        raise requests.exceptions.ConnectionError("接続できません")
    return _get


def _raising_get(message):
    """トークンを含む文面の例外を投げる差し替え。"""
    def _get(url, headers=None, **kwargs):
        raise requests.exceptions.ConnectionError(message)
    return _get


class TokenNotLeakedTest(unittest.TestCase):
    """トークンの値が print / 戻り値のどちらにも現れないこと。"""

    def _run(self, fake_get, call):
        from core.version_manager import VersionManager

        mgr = VersionManager(github_token=self.token)
        out = io.StringIO()
        with unittest.mock.patch("core.version_manager.requests.get", fake_get):
            with contextlib.redirect_stdout(out):
                result = call(mgr)
        return result, out.getvalue()

    def setUp(self):
        self.token = NEWLINE_TOKEN

    def test_newline_token_is_not_written_to_log_or_dialog(self):
        """改行入りトークンでも、値がログにも戻り値にも出ないこと。"""
        recorded = []
        result, logged = self._run(
            _headers_only_get(recorded), lambda m: m.check_for_updates())

        self.assertNotIn(SECRET, logged, "トークンがログへ出ている")
        self.assertNotIn(SECRET, repr(result), "トークンが戻り値へ出ている")
        self.assertTrue(result.get("error"), "失敗を伝えていない")

    def test_token_in_exception_text_is_masked(self):
        """例外文へトークンが混ざっていても、そのまま出さないこと。"""
        self.token = SECRET
        result, logged = self._run(
            _raising_get("proxy rejected 'token %s'" % SECRET),
            lambda m: m.check_for_updates())

        self.assertNotIn(SECRET, logged, "トークンがログへ出ている")
        self.assertNotIn(SECRET, repr(result), "トークンが戻り値へ出ている")

    def test_download_error_does_not_leak_the_token(self):
        """ダウンロード失敗の記録にもトークンを残さないこと。"""
        self.token = SECRET
        tmp = tempfile.mkdtemp(prefix="netbelt-token-")

        def call(mgr):
            mgr.UPDATE_DIR = tmp
            return mgr.download_update("https://example.com/a.zip")

        result, logged = self._run(
            _raising_get("proxy rejected 'token %s'" % SECRET), call)

        self.assertIsNone(result)
        self.assertNotIn(SECRET, logged, "トークンがログへ出ている")

    def test_checksum_fetch_error_does_not_leak_the_token(self):
        """チェックサム取得の失敗でもトークンを残さないこと。"""
        self.token = SECRET
        result, logged = self._run(
            _raising_get("proxy rejected 'token %s'" % SECRET),
            lambda m: m._fetch_expected_sha256("https://example.com/a.sha256"))

        self.assertIsNone(result)
        self.assertNotIn(SECRET, logged, "トークンがログへ出ている")


class BrokenTokenIsRefusedTest(unittest.TestCase):
    """壊れたトークンは、そもそもヘッダへ載せないこと。"""

    def test_control_characters_are_refused(self):
        from core.version_manager import VersionManager

        recorded = []
        mgr = VersionManager(github_token=NEWLINE_TOKEN)
        with unittest.mock.patch("core.version_manager.requests.get",
                                 _headers_only_get(recorded)):
            with contextlib.redirect_stdout(io.StringIO()):
                mgr.check_for_updates()

        self.assertEqual(len(recorded), 1)
        self.assertNotIn("Authorization", recorded[0],
                         "壊れたトークンをヘッダへ載せている")

    def test_surrounding_whitespace_is_trimmed(self):
        """前後の空白だけなら、取り除いて使うこと。"""
        from core.version_manager import VersionManager

        recorded = []
        mgr = VersionManager(github_token="  " + SECRET + "\n")
        with unittest.mock.patch("core.version_manager.requests.get",
                                 _headers_only_get(recorded)):
            with contextlib.redirect_stdout(io.StringIO()):
                mgr.check_for_updates()

        self.assertEqual(recorded[0].get("Authorization"),
                         "token " + SECRET)


if __name__ == "__main__":
    unittest.main()
