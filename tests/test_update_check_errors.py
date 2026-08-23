"""更新の確認に失敗したとき、それを「最新です」と誤って伝えないことを確認する。

check_for_updates() が通信エラーで None を返し、呼び出し側が
`if update_info and update_info.get('available')` の else で
「現在のバージョンは最新です」と表示していたため、更新経路が壊れていても
利用者にはそう見えなかった。公開後の不具合切り分けを難しくする。
"""
import sys
import unittest
import unittest.mock

sys.path.insert(0, "src")


class _Resp:
    def __init__(self, payload=None, status=200):
        self._payload = payload or {}
        self.status_code = status
        self.headers = {}
        self.text = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError("HTTP %d" % self.status_code)

    def json(self):
        return self._payload


class UpdateCheckErrorTest(unittest.TestCase):
    def setUp(self):
        from core.version_manager import VersionManager
        self.mgr = VersionManager()

    def _check(self, side_effect=None, return_value=None):
        with unittest.mock.patch("core.version_manager.requests.get",
                                 side_effect=side_effect,
                                 return_value=return_value):
            return self.mgr.check_for_updates()

    def test_network_error_is_reported_as_error(self):
        """通信エラーは「更新なし」ではなく、失敗として返すこと。"""
        import requests
        got = self._check(side_effect=requests.exceptions.ConnectionError("切断"))
        self.assertIsNotNone(got, "通信エラーで None を返している（呼び出し側が最新と誤解する）")
        self.assertTrue(got.get("error"), "error が設定されていない")
        self.assertFalse(got.get("available"))

    def test_http_error_is_reported_as_error(self):
        got = self._check(return_value=_Resp(status=503))
        self.assertIsNotNone(got)
        self.assertTrue(got.get("error"))

    def test_unexpected_error_is_reported_as_error(self):
        got = self._check(side_effect=ValueError("想定外"))
        self.assertIsNotNone(got)
        self.assertTrue(got.get("error"))

    def test_successful_check_has_no_error(self):
        """正常時は error を立てないこと（既存の挙動を壊さない）。"""
        payload = {
            "tag_name": "v99.0.0",
            "body": "notes",
            "published_at": "2026-08-23T00:00:00Z",
            "assets": [
                {"name": "NetBelt-v99.0.0-Windows-Portable.zip",
                 "url": "https://api.example.com/assets/1"},
            ],
        }
        got = self._check(return_value=_Resp(payload))
        self.assertIsNotNone(got)
        self.assertFalse(got.get("error"), "正常なのに error が立っている")
        self.assertTrue(got.get("available"))
        self.assertEqual(got.get("version"), "99.0.0")

    def test_up_to_date_has_no_error(self):
        """最新の場合も error は立てないこと。"""
        from core.version_manager import VersionManager
        payload = {
            "tag_name": "v" + VersionManager.CURRENT_VERSION,
            "body": "",
            "published_at": "",
            "assets": [],
        }
        got = self._check(return_value=_Resp(payload))
        self.assertFalse(got.get("error"))
        self.assertFalse(got.get("available"))


class UpdateCheckUiWiringTest(unittest.TestCase):
    """UI が「失敗」と「最新です」を区別していること。"""

    def test_main_window_handles_error_branch(self):
        import ast
        import io

        src = io.open("src/ui/main_window.py", encoding="utf-8").read()
        tree = ast.parse(src)

        target = next((n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef)
                       and n.name == "_on_check_for_updates"), None)
        self.assertIsNotNone(target, "_on_check_for_updates が見つからない")

        body = ast.get_source_segment(src, target) or ""
        # 文字列 "error" は except 節にも出るため、実際の分岐を見る
        self.assertIn("get('error')", body,
                      "更新確認の失敗を区別していない（失敗が『最新です』になる）")


if __name__ == "__main__":
    unittest.main()
