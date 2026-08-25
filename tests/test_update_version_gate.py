"""自動更新が「どれを掴むか」「本当に新しいか」を検証する。

2つの問題があった。

  1. ダウンロード済み ZIP の版がどこにも記録されておらず、起動時の
     「未適用の更新があります」は検証済みと24時間以内しか見ていなかった。
     手で入れ直したあとに古い ZIP が残っていると、ダウングレードを勧める。
     しかもダイアログに版が1文字も出ないので、利用者に判断材料が無い。
  2. 併せ置かれる .sha256 を「最初の1つ」で選んでいた。Release に asset を
     1つ足しただけで別物の控えを掴み、照合が必ず外れる。壊れはしないが、
     自動更新が黙って動かなくなる。
"""
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")


def asset(name, url=None):
    return {"name": name, "url": url or ("https://example.com/" + name)}


class AssetSelectionTest(unittest.TestCase):
    """Release の資産から、どれを選ぶか。GitHub API は叩かない。"""

    def _pick(self, assets, latest="9.9.9"):
        """check_for_updates の応答処理だけを、作り物の JSON で通す。"""
        from unittest import mock
        from core.version_manager import VersionManager

        payload = {"tag_name": "v" + latest, "body": "notes", "assets": assets}
        response = mock.Mock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None

        with mock.patch("core.version_manager.requests.get",
                        return_value=response):
            return VersionManager().check_for_updates()

    def test_the_checksum_matches_the_chosen_archive(self):
        """選んだ ZIP に対応する .sha256 を選ぶこと。

        以前は「最初の .sha256」を取っていたので、無関係な asset が
        先に並ぶだけで照合が必ず外れた。
        """
        info = self._pick([
            asset("mibs-bundle.zip.sha256"),
            asset("mibs-bundle.zip"),
            asset("NetBelt-v9.9.9-Windows-Portable.zip"),
            asset("NetBelt-v9.9.9-Windows-Portable.zip.sha256"),
        ])
        self.assertIsNotNone(info)
        self.assertIn("Windows-Portable.zip", info["download_url"])
        self.assertIn("NetBelt-v9.9.9-Windows-Portable.zip.sha256",
                      info["sha256_url"] or "",
                      "別の資産の控えを掴んでいる")

    def test_the_order_of_assets_does_not_matter(self):
        """Windows 向けの ZIP は、何番目にあっても選べること。"""
        for position in (0, 2):
            with self.subTest(position=position):
                assets = [asset("notes.txt"), asset("other.zip")]
                assets.insert(position, asset("NetBelt-Windows-Portable.zip"))
                assets.append(asset("NetBelt-Windows-Portable.zip.sha256"))
                info = self._pick(assets)
                self.assertIn("Windows-Portable.zip", info["download_url"])

    def test_an_unrelated_archive_is_not_taken_as_the_update(self):
        """Windows 向けが無いとき、無関係な ZIP を掴まないこと。

        実行可能物でない ZIP を掴んでも良いことは何もない。
        見つからないことは呼び出し側が扱える。
        """
        info = self._pick([asset("mibs-bundle.zip"),
                           asset("mibs-bundle.zip.sha256")])
        self.assertIsNotNone(info)
        self.assertIsNone(info["download_url"],
                          "無関係な ZIP を更新として選んだ")


class PendingVersionTest(unittest.TestCase):
    """ダウンロード済み ZIP の版を控え、読めること。"""

    def test_the_version_is_recorded_next_to_the_archive(self):
        from core.version_manager import VersionManager
        d = tempfile.mkdtemp(prefix="netbelt-pending-")
        zip_path = os.path.join(d, "NetBelt-update.zip")
        with io.open(zip_path, "wb") as f:
            f.write(b"PK\x03\x04")
        with io.open(zip_path + ".version", "w", encoding="ascii") as f:
            f.write("1.2.3")

        self.assertEqual(VersionManager.pending_version(zip_path), "1.2.3")

    def test_a_missing_record_reads_as_unknown(self):
        from core.version_manager import VersionManager
        d = tempfile.mkdtemp(prefix="netbelt-pending-")
        zip_path = os.path.join(d, "NetBelt-update.zip")
        with io.open(zip_path, "wb") as f:
            f.write(b"PK\x03\x04")

        self.assertIsNone(VersionManager.pending_version(zip_path),
                          "控えが無いのに版を返した")

    def test_download_update_records_the_version(self):
        """ダウンロード成功時に版を控えること。"""
        import hashlib
        from unittest import mock
        from core.version_manager import VersionManager

        payload = b"NetBelt portable archive"
        digest = hashlib.sha256(payload).hexdigest()

        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.headers = {"content-length": str(len(payload))}
        response.iter_content.return_value = [payload]
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)

        d = tempfile.mkdtemp(prefix="netbelt-dl-")
        manager = VersionManager()
        with mock.patch.object(VersionManager, "UPDATE_DIR", d), \
                mock.patch("core.version_manager.requests.get",
                           return_value=response), \
                mock.patch.object(manager, "_fetch_expected_sha256",
                                  return_value=digest):
            zip_path = manager.download_update(
                "https://example.com/NetBelt.zip",
                sha256_url="https://example.com/NetBelt.zip.sha256",
                version="2.5.0")

        self.assertIsNotNone(zip_path, "ダウンロードが成功していない")
        self.assertEqual(VersionManager.pending_version(zip_path), "2.5.0",
                         "版を控えていない")


class VersionGateTest(unittest.TestCase):
    """現在より新しいときだけ勧めること。"""

    def test_an_older_pending_update_is_not_offered(self):
        from core.version_manager import VersionManager
        current = VersionManager.CURRENT_VERSION
        self.assertLessEqual(
            VersionManager.compare_versions("0.0.1", current), 0,
            "比較が期待どおりでない（古い版が新しいと判定された）")

    def test_a_newer_pending_update_is_offered(self):
        from core.version_manager import VersionManager
        current = VersionManager.CURRENT_VERSION
        self.assertGreater(
            VersionManager.compare_versions("999.0.0", current), 0,
            "比較が期待どおりでない（新しい版が古いと判定された）")

    def test_the_same_version_is_not_offered(self):
        """同じ版の ZIP で再起動を促さないこと。"""
        from core.version_manager import VersionManager
        current = VersionManager.CURRENT_VERSION
        self.assertEqual(
            VersionManager.compare_versions(current, current), 0)


if __name__ == "__main__":
    unittest.main()
