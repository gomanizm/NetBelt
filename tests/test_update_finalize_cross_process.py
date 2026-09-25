"""同じ更新を 2 つの NetBelt が保存しても、嘘の成功を返さないことを検証する。

download_update の確定（退避・置き換え・失敗時の戻し）は、同じプロセスの
中では最終名ごとのロックで 1 本ずつになっている（tests/test_update_concurrent_finalize.py）。
ロックはプロセスごとに別の辞書に載るので、別の NetBelt との重なりには効かない。

実測（検査役 cx5j-check-release の p4_driver.py + p4_worker.py、requests.get
だけを差し替えた偽の受信。NetBelt を 2 つ動かした状況を別プロセス 2 本で作り、
ラウンドごとにファイルで待ち合わせ、各ラウンドの更新フォルダには同じ版の
検証済みの組を置いて「取り直し」にした）: 60 ラウンド中 2 回（round 22, 44）、
『成功を返したのに適用できない』。残っていたのは
['NetBelt-1.3.2.zip.sha256', 'NetBelt-1.3.2.zip.version'] だけで ZIP が消えており、
verify_before_apply は『更新ファイルが見つかりません。』を返した。利用者から
見ると、片方の NetBelt は「ダウンロードに失敗しました」、もう片方は
「ダウンロード完了！」と出したのに、適用しようとすると「更新ファイルが
見つかりません」になる。

前提も確かめてある: src に多重起動の抑止は無く、UPDATE_DIR は
%TEMP%\\NetBeltUpdates で全インスタンス共通、最終名も版ごとに固定なので、
2 つの NetBelt が同じ新版を取れば同じ最終名を確定しに行く。

利用者の決定（2026-09-20 / release-04）: プロセス間で排他する。更新フォルダに
md / O_EXCL の目印を作って確定手順を囲み、取れなかった側は数秒待って
『別の NetBelt が同じ更新を保存中です』で失敗させる（嘘の成功を返さない。
保守側の判断）。異常終了で残った目印は、古くなっていれば引き取る。

ここでは、別プロセスの代わりに _finalize_lock を素通りさせた 2 スレッドで
同じ操作順を作る（別プロセスでは、この同一プロセス内のロックがまさに
存在しないため）。操作順は tests/test_update_concurrent_finalize.py が
台本にしているものと同じで、Windows が同じ宛先への置き換えが重なったときに
返す拒否（WinError 5）を、相手がその宛先を置き換えている最中だけ模す。
"""
import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, "src")

URL = "https://api.github.com/repos/example/NetBelt/releases/assets/1"
VERSION = "1.3.2"
BODY = b"PK" + b"A" * 1000

STEP_TIMEOUT = 1.0
BUSY_MESSAGE = "別の NetBelt が同じ更新を保存中です"


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


class _NoLock:
    """別プロセスから見た _finalize_lock（＝何も守らない）。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class CrossProcessFinalizeTest(unittest.TestCase):

    def setUp(self):
        from core.version_manager import VersionManager
        self.VersionManager = VersionManager
        self.tmp = tempfile.mkdtemp(prefix="netbelt-xprocfin-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.printed = []
        for patcher in (
                unittest.mock.patch.object(VersionManager, "UPDATE_DIR", self.tmp),
                unittest.mock.patch("core.version_manager.requests.get", _fake_get),
                unittest.mock.patch("builtins.print", self._record)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _record(self, *args, **kwargs):
        self.printed.append(" ".join(str(a) for a in args))

    def _download(self):
        return self.VersionManager().download_update(
            URL, sha256_url=URL + ".sha256", version=VERSION)

    def _names(self):
        return sorted(os.listdir(self.tmp))

    def _assert_nobody_lied(self, results, zip_path):
        base = os.path.basename(zip_path)
        for who, returned in sorted(results.items()):
            if not returned:
                continue
            problem = self.VersionManager().verify_before_apply(returned, VERSION)
            self.assertIsNone(
                problem,
                "%s が成功を返したのに適用できない（%s）: %s"
                % (who, problem, self._names()))
        self.assertTrue([r for r in results.values() if r],
                        "どちらも失敗した: %s" % (results,))
        self.assertEqual(self._names(),
                         [base, base + ".sha256", base + ".version"],
                         "検証済みの組が欠けたか、一時ファイルが残った")

    def test_two_netbelts_never_both_report_a_save_that_is_not_there(self):
        """別プロセス相当の重なりでも、返した成功が適用できること。"""
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
        inflight = {}
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
            results[threading.current_thread().name] = self._download()

        with unittest.mock.patch("core.version_manager._finalize_lock",
                                 lambda path: _NoLock()), \
             unittest.mock.patch("core.version_manager.os.replace", replace):
            b = threading.Thread(target=worker, name="B", daemon=True)
            b.start()
            self.assertTrue(b_paused.wait(5), "前提が崩れている（B が確定に入らない）")
            a = threading.Thread(target=worker, name="A", daemon=True)
            a.start()
            a_at_sha.wait(STEP_TIMEOUT)
            b_go.set()
            b.join(30)
            a.join(30)
        self.assertFalse(b.is_alive() or a.is_alive(), "確定が終わらない")

        self._assert_nobody_lied(results, zip_path)

    def test_a_marker_from_another_netbelt_fails_the_save(self):
        """別の NetBelt が確定中なら、待ったうえで失敗として返すこと。"""
        zip_path = self._download()
        self.assertIsNotNone(zip_path)
        before = self._names()
        marker = zip_path + ".finalizing"
        open(marker, "wb").close()
        self.addCleanup(os.remove, marker)

        with unittest.mock.patch("core.version_manager.FINALIZE_WAIT_SEC", 0.3):
            started = time.monotonic()
            returned = self._download()
            waited = time.monotonic() - started

        self.assertIsNone(returned, "嘘の成功を返した")
        self.assertGreaterEqual(waited, 0.3, "待たずに諦めた")
        self.assertTrue(any(BUSY_MESSAGE in line for line in self.printed),
                        "理由を伝えていない: %s" % (self.printed[-5:],))
        self.assertEqual(
            self._names(), sorted(before + [os.path.basename(marker)]),
            "既にあった検証済みの組を壊したか、書きかけが残った")

    def test_a_marker_left_by_a_crash_is_taken_over(self):
        """異常終了で残った目印は、古くなっていれば引き取ること。"""
        zip_path = self._download()
        self.assertIsNotNone(zip_path)
        marker = zip_path + ".finalizing"
        open(marker, "wb").close()
        old = time.time() - 2 * 3600
        os.utime(marker, (old, old))

        returned = self._download()

        self.assertEqual(returned, zip_path, self.printed[-5:])
        self.assertFalse(os.path.exists(marker), "目印が残った")
        base = os.path.basename(zip_path)
        self.assertEqual(self._names(),
                         [base, base + ".sha256", base + ".version"])

    def test_the_marker_is_gone_once_the_save_has_finished(self):
        """うまくいった確定でも、目印を残さないこと。"""
        zip_path = self._download()
        self.assertIsNotNone(zip_path)
        self.assertFalse(os.path.exists(zip_path + ".finalizing"))


if __name__ == "__main__":
    unittest.main()
