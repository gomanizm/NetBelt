"""ダウンロードが途中で切れたとき、書きかけを置き去りにしないことを検証する。

download_update は検証を通るまで .zip.part という名前で書く（未検証の
ファイルが「未適用の更新」として拾われ、検証なしで適用されるのを防ぐ
ため）。中止とチェックサム不一致では .part を消しているが、受信中の
通信例外は一番外側の except に落ちて return None するだけで、.part を
残したまま帰る。

掃除する側も拾わない。get_pending_update_files は filename.endswith('.zip')
でしか集めないので、cleanup_old_updates の対象にならない。結果として
%TEMP%\\NetBeltUpdates に書きかけが残り続け、利用者が再試行しない限り
更新 ZIP 一個ぶんの容量が居座る。

なお .part を get_pending_update_files に含めてはいけない。あの口は
「適用できる更新」を返すもので、未検証の断片を適用候補にしてしまう。
掃除は別の経路で行う。
"""
import io
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, "src")


class _ResponseThatDropsMidway:
    """受信の途中で回線が切れる requests の戻り値を模す。"""

    def __init__(self, before_break=b"x" * 8192 * 3):
        self.before_break = before_break
        self.headers = {"content-length": str(len(before_break) * 4)}
        self.status_code = 200

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self.before_break), chunk_size):
            yield self.before_break[i:i + chunk_size]
        raise IOError("接続がリセットされました")


class PartialDownloadCleanupTest(unittest.TestCase):
    def setUp(self):
        from core.version_manager import VersionManager
        self.tmp = tempfile.mkdtemp(prefix="netbelt-part-")
        self.mgr = VersionManager()
        self.mgr.UPDATE_DIR = self.tmp

    def _leftovers(self):
        return sorted(os.listdir(self.tmp))

    def _place_part(self, age_hours=0.0):
        """書きかけのファイルを1つ置く。"""
        path = os.path.join(self.tmp, "NetBelt-update.zip.part")
        with io.open(path, "wb") as f:
            f.write(b"half a zip")
        if age_hours:
            old = time.time() - age_hours * 3600
            os.utime(path, (old, old))
        return path

    def test_a_broken_connection_does_not_leave_a_part_file(self):
        """受信中に切れても、書きかけを残さないこと。"""
        with unittest.mock.patch("core.version_manager.requests.get",
                                 return_value=_ResponseThatDropsMidway()):
            result = self.mgr.download_update("https://example.com/x.zip")

        self.assertIsNone(result, "失敗したのにパスを返している")
        self.assertEqual(self._leftovers(), [],
                         "書きかけが残っている: %s" % self._leftovers())

    def test_an_old_leftover_part_is_swept_up(self):
        """既に残ってしまった書きかけを、掃除で片付けられること。"""
        self._place_part(age_hours=48)
        self.mgr.cleanup_old_updates(max_age_hours=24)
        self.assertEqual(self._leftovers(), [],
                         "古い書きかけが掃除で消えない: %s" % self._leftovers())

    def test_a_recent_part_is_left_alone(self):
        """進行中かもしれない書きかけまで消さないこと。"""
        self._place_part(age_hours=0)
        self.mgr.cleanup_old_updates(max_age_hours=24)
        self.assertEqual(self._leftovers(), ["NetBelt-update.zip.part"],
                         "進行中の可能性があるものまで消している")

    def test_a_part_file_is_not_offered_as_a_pending_update(self):
        """書きかけを「適用できる更新」として拾わないこと。

        拾うと、検証を通っていない断片を適用してしまう。
        """
        self._place_part()
        self.assertEqual(self.mgr.get_pending_update_files(), [],
                         "未検証の書きかけを適用候補にしている")

    def _place_zip_with_sidecars(self, age_hours=48.0):
        """検証を通った ZIP と、その傍らのファイル一式を置く。"""
        base = os.path.join(self.tmp, "NetBelt-update.zip")
        for path in (base, base + ".sha256", base + ".version"):
            with io.open(path, "wb") as f:
                f.write(b"x")
            old = time.time() - age_hours * 3600
            os.utime(path, (old, old))
        return base

    def test_removing_an_expired_zip_takes_its_sidecars_with_it(self):
        """期限切れの ZIP を消すとき、傍らのファイルも片付けること。

        download_update は検証を通った ZIP の隣に .sha256 と .version を
        書く。掃除が ZIP しか見ないと、この2つが孤児として残り続ける
        （適用したときは updater.bat が3つとも消すので、適用しなかった
        ぶんだけ溜まる）。
        """
        self._place_zip_with_sidecars(age_hours=48)
        self.mgr.cleanup_old_updates(max_age_hours=24)
        self.assertEqual(self._leftovers(), [],
                         "傍らのファイルが残っている: %s" % self._leftovers())

    def test_a_recent_zip_keeps_its_sidecars(self):
        """まだ期限内の更新は、傍らのファイルごと残すこと。"""
        self._place_zip_with_sidecars(age_hours=0)
        self.mgr.cleanup_old_updates(max_age_hours=24)
        self.assertEqual(len(self._leftovers()), 3,
                         "期限内の更新まで消している: %s" % self._leftovers())

    def test_a_finished_zip_is_still_offered(self):
        """検証を通った ZIP はこれまでどおり拾うこと。"""
        path = os.path.join(self.tmp, "NetBelt-update.zip")
        io.open(path, "wb").write(b"PK\x03\x04")
        self.assertEqual(self.mgr.get_pending_update_files(), [path])


if __name__ == "__main__":
    unittest.main()
