"""確定の途中でプロセスが終わったときの控えも、掃除で片付くことを検証する。

download_update は、検証を通った ZIP を最終名にする前に、書きかけの名前
（<zip>.<pid>-<tid>.part）の傍らへ控え（.part.sha256 / .part.version）を
書く。同じ版を取り直すときは、既にある組を退避名
（.prev.part / .prev.part.sha256 / .prev.part.version）へ移してから
置き換える。どれも途中でプロセスが終わると取り残される。

cleanup_old_updates が拾っていたのは、名前が '.part' で終わるものだけ
だった。実測（cx5b-verify-release の r03_probe.py の 3)、62357d1）:
退避の直後にプロセスが終わった状態を作り、全部を 48 時間前の日付にして
cleanup_old_updates(24) を呼ぶと、.part と .prev.part は消えたが
.part.sha256・.part.version・.prev.part.sha256・.prev.part.version の
4 つが残った。どれも .zip ではないので未適用の更新としても拾われず、
このあと誰も消さない。

直し方: 掃除の対象を、名前が '.part' / '.part.sha256' / '.part.version'
で終わるものに広げる。古いものだけを対象にする点は変えない（まだ
書いている最中かもしれないため）。
"""
import hashlib
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, "src")

URL = "https://api.github.com/repos/example/NetBelt/releases/assets/1"
VERSION = "1.3.2"
BODY = b"PK" + b"payload" * 100


class _Response:
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


def _fake_get(url, **kwargs):
    if str(url).endswith(".sha256"):
        return _Response(text=hashlib.sha256(BODY).hexdigest())
    return _Response(content=BODY)


class _ProcessDied(BaseException):
    """プロセスの終了の代わり。except Exception では捕まらない。"""


class PartSidecarCleanupTest(unittest.TestCase):

    def setUp(self):
        from core.version_manager import VersionManager
        self.tmp = tempfile.mkdtemp(prefix="netbelt-partside-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = unittest.mock.patch.object(VersionManager, "UPDATE_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mgr = VersionManager()

    def _download(self):
        with unittest.mock.patch("core.version_manager.requests.get", _fake_get), \
                unittest.mock.patch("builtins.print"):
            return self.mgr.download_update(
                URL, sha256_url=URL + ".sha256", version=VERSION)

    def _die_at_replace(self, nth):
        """nth 番目の os.replace でプロセスが終わったことにして取り直す。"""
        real_replace = os.replace
        calls = {"n": 0}

        def replace(src, dst, *a, **k):
            calls["n"] += 1
            if calls["n"] == nth:
                raise _ProcessDied()
            return real_replace(src, dst, *a, **k)

        with unittest.mock.patch("core.version_manager.os.replace", replace):
            with self.assertRaises(_ProcessDied):
                self._download()

    def _age_everything(self, hours):
        old = time.time() - hours * 3600
        for name in os.listdir(self.tmp):
            os.utime(os.path.join(self.tmp, name), (old, old))

    def _names(self):
        return sorted(os.listdir(self.tmp))

    def test_leftovers_of_a_death_after_stashing_are_swept_up(self):
        """退避の直後に終わった取り直しの残りが、控えまで全部片付くこと。"""
        self.assertIsNotNone(self._download())
        # 退避 3 回（ZIP・.sha256・.version）の直後、最初の置き換えで終わる
        self._die_at_replace(4)
        left = self._names()
        self.assertTrue(any(n.endswith(".prev.part.sha256") for n in left),
                        "前提が崩れている（退避名の控えができていない）: %s" % left)
        self.assertTrue(any(n.endswith(".part.version")
                            and ".prev." not in n for n in left),
                        "前提が崩れている（書きかけの控えができていない）: %s" % left)

        self._age_everything(48)
        with unittest.mock.patch("builtins.print"):
            self.mgr.cleanup_old_updates(24)

        self.assertEqual(self._names(), [],
                         "24 時間を過ぎた書きかけ・退避名の控えが残った")

    def test_leftovers_of_a_death_before_finalizing_are_swept_up(self):
        """最初の確定で終わった場合の書きかけの控えも片付くこと。"""
        self._die_at_replace(1)
        left = self._names()
        self.assertTrue(any(n.endswith(".part.sha256") for n in left),
                        "前提が崩れている（書きかけの控えができていない）: %s" % left)

        self._age_everything(48)
        with unittest.mock.patch("builtins.print"):
            self.mgr.cleanup_old_updates(24)

        self.assertEqual(self._names(), [],
                         "24 時間を過ぎた書きかけの控えが残った")

    def test_recent_leftovers_are_left_alone(self):
        """まだ新しい書きかけと控えは、書いている最中かもしれないので残すこと。"""
        self._die_at_replace(1)
        before = self._names()

        with unittest.mock.patch("builtins.print"):
            self.mgr.cleanup_old_updates(24)

        self.assertEqual(self._names(), before)

    def test_orphaned_sidecars_of_an_interrupted_finalize_are_swept_up(self):
        """本体の .zip を失った控えも、保持期間を過ぎたら片付くこと。

        実測（検査役 cx5c-verify-release の c_leftover_sweep.py、8b0c94e）:
        取り直しの退避の 2 回目（.sha256）でプロセスが終わったことにすると、
        本体の ZIP だけが退避名へ移り、NetBelt-1.3.2.zip.sha256 と
        NetBelt-1.3.2.zip.version が本体の無いまま残った。48 時間前の日付に
        して cleanup_old_updates(24) を呼んでも、名前が '.zip' でも '.part'
        でもないので拾われず、このあと誰も消さない。
        """
        zip_path = self._download()
        self.assertIsNotNone(zip_path)
        # 退避 1 回目（ZIP）の直後、.sha256 の退避で終わる
        self._die_at_replace(2)
        base = os.path.basename(zip_path)
        left = self._names()
        self.assertNotIn(base, left,
                         "前提が崩れている（本体がまだある）: %s" % left)
        self.assertIn(base + ".sha256", left,
                      "前提が崩れている（孤児の控えができていない）: %s" % left)
        self.assertIn(base + ".version", left,
                      "前提が崩れている（孤児の控えができていない）: %s" % left)

        self._age_everything(48)
        with unittest.mock.patch("builtins.print"):
            self.mgr.cleanup_old_updates(24)

        self.assertEqual(self._names(), [],
                         "本体の .zip が無い控えが残った")

    def test_recent_orphaned_sidecars_are_left_alone(self):
        """本体を失った控えでも、まだ新しいうちは触れないこと。

        確定の最中は、本体が退避名へ移っている一瞬だけ控えが孤児に見える。
        そこで消すと、確定しようとしている更新の控えを奪ってしまう。
        """
        self.assertIsNotNone(self._download())
        self._die_at_replace(2)
        before = self._names()

        with unittest.mock.patch("builtins.print"):
            self.mgr.cleanup_old_updates(24)

        self.assertEqual(self._names(), before)

    def test_the_sidecars_of_a_kept_update_are_not_swept(self):
        """掃除の対象を広げても、残している更新の控えには触れないこと。"""
        zip_path = self._download()
        self.assertIsNotNone(zip_path)
        self._die_at_replace(1)
        self._age_everything(48)
        # 残している更新そのものは新しい
        now = time.time()
        for suffix in ("", ".sha256", ".version"):
            os.utime(zip_path + suffix, (now, now))

        with unittest.mock.patch("builtins.print"):
            self.mgr.cleanup_old_updates(24)

        base = os.path.basename(zip_path)
        self.assertEqual(self._names(),
                         [base, base + ".sha256", base + ".version"])
        self.assertIsNone(self.mgr.verify_before_apply(zip_path, VERSION))


if __name__ == "__main__":
    unittest.main()
