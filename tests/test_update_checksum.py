"""自動更新でダウンロードした ZIP の SHA-256 を検証することを確認する。

検証が無いと、通信の破損や不完全なダウンロードに気づかないまま
展開・上書き・再起動まで進んでしまう。

なおハッシュはリリースと同じ場所から取得するため、GitHub 自体が
侵害された場合の改ざんは検知できない。ここで防げるのは主に
「壊れたファイルを掴むこと」である。
"""
import hashlib
import io
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, "src")

CONTENT = b"PK\x03\x04" + b"pretend-this-is-a-zip" * 100
GOOD = hashlib.sha256(CONTENT).hexdigest()
BAD = "0" * 64


class _Resp:
    """requests.get の戻り値を模す。"""

    def __init__(self, content=b"", text="", status=200):
        self.content = content
        self.text = text
        self.status_code = status
        self.headers = {"content-length": str(len(content))}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise IOError("HTTP %d" % self.status_code)

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]


class UpdateChecksumTest(unittest.TestCase):
    def setUp(self):
        from core.version_manager import VersionManager
        self.mgr = VersionManager()
        self.tmp = tempfile.mkdtemp(prefix="netbelt-upd-")
        self.mgr.UPDATE_DIR = self.tmp

    def _patched_get(self, sha_text):
        """ZIP 本体とハッシュファイルの2種類を返す requests.get を作る。"""
        def fake_get(url, **kwargs):
            if str(url).endswith(".sha256"):
                return _Resp(text=sha_text)
            return _Resp(content=CONTENT)
        return fake_get

    def test_matching_checksum_keeps_file(self):
        with unittest.mock.patch("core.version_manager.requests.get",
                                 side_effect=self._patched_get(GOOD)):
            path = self.mgr.download_update("https://example.com/NetBelt.zip",
                                            sha256_url="https://example.com/NetBelt.zip.sha256")
        self.assertIsNotNone(path, "正しいハッシュなのに失敗した")
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), CONTENT)

    def test_mismatched_checksum_is_rejected(self):
        with unittest.mock.patch("core.version_manager.requests.get",
                                 side_effect=self._patched_get(BAD)):
            path = self.mgr.download_update("https://example.com/NetBelt.zip",
                                            sha256_url="https://example.com/NetBelt.zip.sha256")
        self.assertIsNone(path, "ハッシュが一致しないのに受け入れた")
        self.assertEqual(os.listdir(self.tmp), [],
                         "検証に失敗したファイルが残っている")

    def test_missing_checksum_is_rejected(self):
        """ハッシュが取得できない場合は更新を受け入れないこと。"""
        with unittest.mock.patch("core.version_manager.requests.get",
                                 side_effect=self._patched_get(GOOD)):
            path = self.mgr.download_update("https://example.com/NetBelt.zip",
                                            sha256_url=None)
        self.assertIsNone(path, "ハッシュ無しの更新を受け入れた")

    def test_checksum_file_with_filename_suffix(self):
        """`<hash>  <ファイル名>` 形式（sha256sum の出力）も読めること。"""
        text = "%s  NetBelt-v1.0.0-Windows-Portable.zip\n" % GOOD
        with unittest.mock.patch("core.version_manager.requests.get",
                                 side_effect=self._patched_get(text)):
            path = self.mgr.download_update("https://example.com/NetBelt.zip",
                                            sha256_url="https://example.com/NetBelt.zip.sha256")
        self.assertIsNotNone(path)

    def test_checksum_comparison_is_case_insensitive(self):
        with unittest.mock.patch("core.version_manager.requests.get",
                                 side_effect=self._patched_get(GOOD.upper())):
            path = self.mgr.download_update("https://example.com/NetBelt.zip",
                                            sha256_url="https://example.com/NetBelt.zip.sha256")
        self.assertIsNotNone(path, "大文字のハッシュを一致と見なさなかった")

    def test_check_for_updates_reports_sha256_asset(self):
        """リリースの assets から .sha256 を拾って返すこと。"""
        release = {
            "tag_name": "v99.0.0",
            "body": "notes",
            "published_at": "2026-08-23T00:00:00Z",
            "assets": [
                {"name": "NetBelt-v99.0.0-Windows-Portable.zip",
                 "url": "https://api.example.com/assets/1"},
                {"name": "NetBelt-v99.0.0-Windows-Portable.zip.sha256",
                 "url": "https://api.example.com/assets/2"},
            ],
        }

        class _Json(_Resp):
            def json(self):
                return release

        with unittest.mock.patch("core.version_manager.requests.get",
                                 return_value=_Json()):
            info = self.mgr.check_for_updates()

        self.assertIsNotNone(info)
        self.assertEqual(info.get("download_url"), "https://api.example.com/assets/1")
        self.assertEqual(info.get("sha256_url"), "https://api.example.com/assets/2")


if __name__ == "__main__":
    unittest.main()
