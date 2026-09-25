"""更新ファイルの確定（最終名への置き換え）に失敗しても、以前の検証済みの組を失わないこと。

download_update は受信と検証を .part 名で済ませ、最後に os.replace で
ZIP・.sha256・.version を順に最終名へ置き換える。その途中で例外が出ると、
今回作った一時ファイルだけでなく、既にあった最終名の ZIP と両方の控えまで
無条件に消していた。

実測（検証役 release-03）: 同じ版 1.3.2 を 2 回ダウンロードし、2 回目の
1・2・3 番目の os.replace に PermissionError を注入すると、どれも
return None のうえ、files=[] で verify_before_apply が
「更新ファイルが見つかりません。」になった。1 番目は既存のファイルに
一切触れていない段階の失敗だが、それでも検証済みの組が 3 つとも消えた。
最終名の ZIP を別の読み手（二重起動した NetBelt の is_verified_update など）が
開いたまま取り直すと、置き換えは [WinError 5] で失敗し、ZIP の削除も
WinError 32 で失敗して .sha256 と .version だけが消えた。残った ZIP は
検証できず、「一致しません。もう一度ダウンロードしてください」になった。

直し方: 置き換える前に、既にある最終名のファイルを退避名へ移しておく。
途中で失敗したら、今回置いた分と一時ファイルだけを消し、退避したものを
元の名前へ戻す。退避の段階で失敗した（既存の ZIP を誰かが開いている）
場合は、既存の組には手を付けずに終わる。成功したら退避を消す。
"""
import hashlib
import io
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, "src")

URL = "https://api.github.com/repos/example/NetBelt/releases/assets/1"
VERSION = "1.3.2"
BODY_A = b"PK" + b"payload-A" * 100
BODY_B = b"PK" + b"payload-B" * 100


class _Resp:
    def __init__(self, content=b"", text=""):
        self.content = content
        self.text = text
        self.status_code = 200
        self.headers = {"content-length": str(len(content))}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        yield self.content

    def close(self):
        pass


def _fake_get(body):
    def get(url, **kwargs):
        if str(url).endswith(".sha256"):
            return _Resp(text=hashlib.sha256(body).hexdigest())
        return _Resp(content=body)
    return get


class _ReplaceFailingOnce:
    """宛先が target の os.replace を、最初の 1 回だけ失敗させる。

    旧実装では、ZIP・.sha256・.version を宛先とする置き換えが、
    それぞれ 1・2・3 番目の os.replace だった。
    """

    def __init__(self, target):
        self.target = os.path.normcase(os.path.abspath(target))
        self.real = os.replace
        self.fired = False

    def __call__(self, src, dst, *args, **kwargs):
        if (not self.fired
                and os.path.normcase(os.path.abspath(dst)) == self.target):
            self.fired = True
            raise PermissionError(13, "injected: access denied", str(dst))
        return self.real(src, dst, *args, **kwargs)


class FinalizeFailureTest(unittest.TestCase):
    def setUp(self):
        from core.version_manager import VersionManager
        self.tmp = tempfile.mkdtemp(prefix="netbelt-finalize-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = unittest.mock.patch.object(
            VersionManager, "UPDATE_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mgr = VersionManager()

    def _download(self, body):
        with unittest.mock.patch("core.version_manager.requests.get",
                                 _fake_get(body)):
            return self.mgr.download_update(
                URL, sha256_url=URL + ".sha256", version=VERSION)

    def _first_download(self):
        first = self._download(BODY_A)
        self.assertIsNotNone(first, "前提: 1回目は成功する")
        self.assertIsNone(self.mgr.verify_before_apply(first, VERSION),
                          "前提: 1回目は適用できる")
        return first

    def _names(self):
        return sorted(os.listdir(self.tmp))

    def _assert_the_first_set_survives(self, first):
        base = os.path.basename(first)
        self.assertEqual(self._names(),
                         [base, base + ".sha256", base + ".version"],
                         "以前の組が欠けたか、一時ファイルが残った")
        with io.open(first, "rb") as f:
            self.assertEqual(f.read(), BODY_A, "以前の ZIP の中身が変わった")
        self.assertIsNone(
            self.mgr.verify_before_apply(first, VERSION),
            "失敗した取り直しの巻き添えで、使えていた更新が使えなくなった")

    def _redownload_failing_at(self, first, suffix):
        failing = _ReplaceFailingOnce(first + suffix)
        with unittest.mock.patch("core.version_manager.os.replace", failing):
            second = self._download(BODY_B)
        self.assertTrue(failing.fired, "前提: 注入した失敗が起きていない")
        return second

    def test_a_failure_placing_the_zip_keeps_the_verified_set(self):
        """ZIP の置き換え（旧実装の 1 番目）で失敗しても、以前の組が残ること。"""
        first = self._first_download()

        second = self._redownload_failing_at(first, "")

        self.assertIsNone(second)
        self._assert_the_first_set_survives(first)

    def test_a_failure_placing_the_checksum_keeps_the_verified_set(self):
        """.sha256 の置き換え（旧実装の 2 番目）で失敗しても、以前の組に戻ること。"""
        first = self._first_download()

        second = self._redownload_failing_at(first, ".sha256")

        self.assertIsNone(second)
        self._assert_the_first_set_survives(first)

    def test_a_failure_placing_the_version_keeps_the_verified_set(self):
        """.version の置き換え（旧実装の 3 番目）で失敗しても、以前の組に戻ること。"""
        first = self._first_download()

        second = self._redownload_failing_at(first, ".version")

        self.assertIsNone(second)
        self._assert_the_first_set_survives(first)

    def test_a_zip_held_open_by_another_reader_keeps_the_verified_set(self):
        """最終名の ZIP を別の読み手が開いていても、以前の組が残ること。

        Python の open は削除共有なしで開くので、その ZIP は改名も削除も
        できない（二重起動した NetBelt がハッシュを取っている最中に相当）。
        """
        first = self._first_download()

        with io.open(first, "rb"):
            second = self._download(BODY_B)

        self.assertIsNone(second)
        self._assert_the_first_set_survives(first)

    def test_a_successful_redownload_replaces_the_set_cleanly(self):
        """成功した取り直しは新しい組に置き換わり、退避も一時ファイルも残さないこと。"""
        first = self._first_download()

        second = self._download(BODY_B)

        self.assertEqual(second, first)
        base = os.path.basename(first)
        self.assertEqual(self._names(),
                         [base, base + ".sha256", base + ".version"])
        with io.open(second, "rb") as f:
            self.assertEqual(f.read(), BODY_B)
        self.assertIsNone(self.mgr.verify_before_apply(second, VERSION))


if __name__ == "__main__":
    unittest.main()
