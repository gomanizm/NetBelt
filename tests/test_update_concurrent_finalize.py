"""同じ版のダウンロード 2 本が同時に確定しても、検証済みの組を失わないことを検証する。

download_update は検証を通った ZIP・.sha256・.version を、既にある組を
退避名へ移してから最終名へ置き換え、途中で失敗したら今回置いた分を消して
退避した組を戻す（tests/test_update_finalize_failure.py）。この手順は
1 本ずつ走る前提で、同じ最終名を確定する 2 本が重なると、互いの組を
退避したり消したりする。

実測（cx5b-check-release の r03_concurrent.py、62357d1）: 同じ版の
取り直しを 2 スレッドで同時に走らせると、600 回中 4 回、片方が成功を
返したのに最終名の ZIP が無くなっていた。操作列の一例:
T2 が ZIP を退避 → T1 が .sha256 と .version を退避 → T2・T1 の順に
ZIP を置く → T1 の .sha256 の置き換えが T2 の置き換えと重なって
WinError 5 → T1 は後始末で「自分が置いた」ZIP を消す（実際には T2 が
成功として返した ZIP）→ T2 は退避していた元の組を捨てる。残るのは
.sha256 と .version だけで、検証済みの組はどこにも無い。

直し方: 同じプロセスの中では、最終名ごとのロックで確定の手順
（退避・置き換え・失敗時の戻し・退避の破棄）を 1 本ずつにする。
別の NetBelt（別プロセス）との重なりはこのロックでは防げない。

ここでは、重なったときの操作順を台本どおりに進め、Windows が同じ
宛先への置き換えが重なったときに返す拒否（WinError 5）を、相手が
その宛先を置き換えている最中だけ模す。確定が 1 本ずつになっていれば
相手は途中に割り込めず、台本の待ちは時間切れで先へ進む。
"""
import hashlib
import os
import shutil
import sys
import tempfile
import threading
import unittest
import unittest.mock

sys.path.insert(0, "src")

URL = "https://api.github.com/repos/example/NetBelt/releases/assets/1"
VERSION = "1.3.2"
BODY = b"PK" + b"A" * 1000

# 台本の各段で相手を待つ上限（秒）。確定が 1 本ずつなら相手は来ないので、
# この時間で見切って先へ進む。
STEP_TIMEOUT = 1.0


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


class ConcurrentFinalizeTest(unittest.TestCase):

    def setUp(self):
        from core.version_manager import VersionManager
        self.VersionManager = VersionManager
        self.tmp = tempfile.mkdtemp(prefix="netbelt-concfin-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for patcher in (
                unittest.mock.patch.object(VersionManager, "UPDATE_DIR", self.tmp),
                unittest.mock.patch("core.version_manager.requests.get", _fake_get),
                unittest.mock.patch("builtins.print")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _download(self):
        return self.VersionManager().download_update(
            URL, sha256_url=URL + ".sha256", version=VERSION)

    def _names(self):
        return sorted(os.listdir(self.tmp))

    def _assert_the_verified_set_survives(self, results):
        returned = [r for r in results.values() if r]
        self.assertTrue(returned, "どちらも失敗した: %s" % results)
        zip_path = returned[0]
        problem = self.VersionManager().verify_before_apply(zip_path, VERSION)
        self.assertIsNone(problem, "成功を返した更新が適用できない（%s）: %s"
                          % (problem, self._names()))
        base = os.path.basename(zip_path)
        self.assertEqual(self._names(),
                         [base, base + ".sha256", base + ".version"],
                         "検証済みの組が欠けたか、一時ファイルが残った")

    def test_overlapping_finalizations_keep_the_verified_set(self):
        """重なった確定の片方が失敗しても、もう片方が返した組が残ること。"""
        zip_path = self._download()
        self.assertIsNotNone(zip_path)
        finals = {os.path.normcase(zip_path + s): s
                  for s in ("", ".sha256", ".version")}

        b_paused = threading.Event()   # B が元の組を退避し終え、ZIP を置く直前
        b_go = threading.Event()
        a_at_sha = threading.Event()   # A が .sha256 を置く直前
        b_on_sha = threading.Event()   # B が .sha256 を置き換えている最中
        a_done_sha = threading.Event()
        guard = threading.Lock()
        inflight = {}                  # 宛先 -> 置き換えている最中のスレッド名
        real_replace = os.replace

        def replace(src, dst, *a, **k):
            who = threading.current_thread().name
            role = finals.get(os.path.normcase(dst))
            if role == "" and who == "B" and not b_paused.is_set():
                b_paused.set()
                b_go.wait(5)
            elif role == ".sha256" and who == "B":
                with guard:
                    inflight[".sha256"] = who
                b_on_sha.set()
                a_done_sha.wait(STEP_TIMEOUT)
                try:
                    return real_replace(src, dst, *a, **k)
                finally:
                    with guard:
                        inflight.pop(".sha256", None)
            elif role == ".sha256" and who == "A":
                a_at_sha.set()
                b_go.set()
                b_on_sha.wait(STEP_TIMEOUT * 2)
                with guard:
                    busy = inflight.get(".sha256") not in (None, who)
                if busy:
                    # 相手が同じ宛先を置き換えている最中。Windows は拒否する。
                    a_done_sha.set()
                    raise PermissionError(13, "Access is denied", dst)
            return real_replace(src, dst, *a, **k)

        results = {}

        def worker():
            name = threading.current_thread().name
            results[name] = self._download()

        with unittest.mock.patch("core.version_manager.os.replace", replace):
            b = threading.Thread(target=worker, name="B", daemon=True)
            b.start()
            self.assertTrue(b_paused.wait(5), "前提が崩れている（B が確定に入らない）")
            a = threading.Thread(target=worker, name="A", daemon=True)
            a.start()
            a_at_sha.wait(STEP_TIMEOUT)
            b_go.set()
            b.join(15)
            a.join(15)
        self.assertFalse(b.is_alive() or a.is_alive(), "確定が終わらない")

        self._assert_the_verified_set_survives(results)

    def test_simultaneous_redownloads_keep_the_verified_set(self):
        """同じ版の取り直しを同時に何度くり返しても、組が欠けないこと（実際の置き換え）。"""
        zip_path = self._download()
        self.assertIsNotNone(zip_path)
        broken = []
        for round_no in range(500):
            results = {}
            barrier = threading.Barrier(2)

            def worker():
                barrier.wait()
                results[threading.current_thread().name] = self._download()

            threads = [threading.Thread(target=worker, name=n, daemon=True)
                       for n in ("T1", "T2")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(15)
            problem = self.VersionManager().verify_before_apply(zip_path, VERSION)
            names = self._names()
            base = os.path.basename(zip_path)
            if problem or names != [base, base + ".sha256", base + ".version"]:
                broken.append((round_no, {k: bool(v) for k, v in results.items()},
                               problem, names))
                for n in names:
                    os.remove(os.path.join(self.tmp, n))
                self.assertIsNotNone(self._download())
        self.assertEqual(broken, [], "同時の取り直しで検証済みの組が欠けた")


if __name__ == "__main__":
    unittest.main()
