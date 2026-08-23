"""更新ファイルの完全性検証が迂回できないことを確認する。

検証前のファイルを最終的な .zip 名で書いていると、ダウンロードを中断したときに
未検証の部分ファイルが .zip として残り、次回起動時の「未適用の更新」に拾われて
検証なしで適用できてしまう（get_pending_update_files は *.zip を無条件に拾う）。

そのため検証が通るまでは .part 名で書き、通ってから .zip へ改名する。
あわせて検証済みの証を .sha256 として残し、適用時にもう一度確かめる。
"""
import hashlib
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, "src")

CONTENT = b"PK\x03\x04" + b"netbelt-update-payload" * 200
GOOD = hashlib.sha256(CONTENT).hexdigest()
BAD = "1" * 64


class _Resp:
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


class UpdateIntegrityTest(unittest.TestCase):
    def setUp(self):
        from core.version_manager import VersionManager
        self.mgr = VersionManager()
        self.tmp = tempfile.mkdtemp(prefix="netbelt-integrity-")
        self.mgr.UPDATE_DIR = self.tmp

    def _get(self, sha_text):
        def fake_get(url, **kwargs):
            if str(url).endswith(".sha256"):
                return _Resp(text=sha_text)
            return _Resp(content=CONTENT)
        return fake_get

    def _download(self, sha_text=GOOD, **kw):
        with unittest.mock.patch("core.version_manager.requests.get",
                                 side_effect=self._get(sha_text)):
            return self.mgr.download_update(
                "https://example.com/NetBelt.zip",
                sha256_url="https://example.com/NetBelt.zip.sha256", **kw)

    def _listing(self):
        return sorted(os.listdir(self.tmp))

    # --- 検証を通ったときだけ .zip になる ---

    def test_verified_download_produces_zip_and_sidecar(self):
        path = self._download()
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith(".zip"))
        names = self._listing()
        self.assertIn(os.path.basename(path), names)
        self.assertIn(os.path.basename(path) + ".sha256", names,
                      "検証済みの控えが残っていない")
        self.assertFalse([n for n in names if n.endswith(".part")],
                         ".part が残っている")

    def test_failed_checksum_leaves_no_zip(self):
        """照合に失敗したら .zip を作らないこと（本件の核心）。"""
        path = self._download(sha_text=BAD)
        self.assertIsNone(path)
        self.assertEqual([n for n in self._listing() if n.endswith(".zip")], [],
                         "未検証のファイルが .zip として残っている")
        self.assertEqual(self._listing(), [], "一時ファイルが残っている")

    def test_cancelled_download_leaves_no_zip(self):
        """中断しても未検証のファイルを .zip として残さないこと。"""
        path = self._download(cancel_check=lambda: True)
        self.assertIsNone(path)
        self.assertEqual([n for n in self._listing() if n.endswith(".zip")], [],
                         "中断したのに .zip が残っている")

    def test_pending_scan_does_not_see_unverified_file(self):
        """未検証のファイルは「未適用の更新」に拾われないこと。"""
        self._download(sha_text=BAD)
        self.assertEqual(self.mgr.get_pending_update_files(), [])

    # --- 適用前の再確認 ---

    def test_is_verified_update_accepts_verified_pair(self):
        path = self._download()
        self.assertTrue(self.mgr.is_verified_update(path))

    def test_is_verified_update_rejects_missing_sidecar(self):
        path = self._download()
        os.remove(path + ".sha256")
        self.assertFalse(self.mgr.is_verified_update(path),
                         "控えが無いのに検証済みと判定した")

    def test_is_verified_update_rejects_tampered_file(self):
        """検証後に中身を差し替えられたら弾くこと。"""
        path = self._download()
        with open(path, "ab") as f:
            f.write(b"TAMPERED")
        self.assertFalse(self.mgr.is_verified_update(path),
                         "改変されたファイルを検証済みと判定した")

    def test_is_verified_update_rejects_planted_zip(self):
        """外から置かれただけの ZIP は検証済みと見なさないこと。"""
        planted = os.path.join(self.tmp, "NetBelt-v9.9.9-Windows-Portable.zip")
        with open(planted, "wb") as f:
            f.write(b"PK\x03\x04planted")
        self.assertIn(planted, self.mgr.get_pending_update_files())
        self.assertFalse(self.mgr.is_verified_update(planted),
                         "置かれただけの ZIP を検証済みと判定した")


if __name__ == "__main__":
    unittest.main()
