"""ダウンロードした更新が、版ごとに別のファイルになることを確認する。

GitHub の asset は API 形式の URL（末尾は asset 番号）で取るので、
basename からは何も分からず、全ての版が同じ NetBelt-update.zip を
共有していた。実測（inspector, release #7）:
`basename of API-style url: '1'` / `path A: ...NetBelt-update.zip` /
`path B: ...NetBelt-update.zip` / `same path: True`、そして 9.9.1 を
表示したダイアログが `content now: b'exe-B-v9.9.2'` の ZIP を
updater.bat へ渡した。

同時に受信すると、書きかけ (.part) まで共有するため両方が失敗し、
直前に検証を通っていた ZIP まで消えた（`concurrent results:
{'A': None, 'B': None}` / `files left: ['NetBelt-update.zip.sha256',
'NetBelt-update.zip.version']`）。
"""
import hashlib
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, "src")

# GitHub の asset はこの形（末尾は asset の番号で、名前は入らない）
URL_A = "https://api.github.com/repos/example/NetBelt/releases/assets/1"
URL_B = "https://api.github.com/repos/example/NetBelt/releases/assets/2"
BODY_A = b"PK" + b"payload-for-9.9.1" * 50
BODY_B = b"PK" + b"payload-for-9.9.2" * 50


class _Resp:
    """細切れに返す応答。同時受信の重なりを作るために少し待つ。"""

    def __init__(self, content=b"", text="", slow=False):
        self.content = content
        self.text = text
        self.status_code = 200
        self.headers = {"content-length": str(len(content))}
        self.slow = slow

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        step = max(1, len(self.content) // 4)
        for i in range(0, len(self.content), step):
            if self.slow:
                time.sleep(0.02)
            yield self.content[i:i + step]


def _fake_get(slow=False):
    def get(url, **kwargs):
        if str(url).endswith(".sha256"):
            body = BODY_A if url.startswith(URL_A) else BODY_B
            return _Resp(text=hashlib.sha256(body).hexdigest())
        return _Resp(content=BODY_A if url == URL_A else BODY_B, slow=slow)
    return get


class DownloadNamingTest(unittest.TestCase):
    def setUp(self):
        from core.version_manager import VersionManager
        self.tmp = tempfile.mkdtemp(prefix="netbelt-naming-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mgr = VersionManager()
        self.mgr.UPDATE_DIR = self.tmp

    def _download(self, url, version, mgr=None, results=None, key=None,
                  slow=False):
        mgr = mgr or self.mgr
        path = mgr.download_update(url, sha256_url=url + ".sha256",
                                   version=version)
        if results is not None:
            results[key] = path
        return path

    def test_two_versions_do_not_share_one_file(self):
        """後から来た版が、先の版の ZIP を置き換えないこと。"""
        with unittest.mock.patch("core.version_manager.requests.get",
                                 side_effect=_fake_get()):
            path_a = self._download(URL_A, "9.9.1")
            path_b = self._download(URL_B, "9.9.2")

        self.assertIsNotNone(path_a)
        self.assertIsNotNone(path_b)
        self.assertNotEqual(path_a, path_b, "版が違うのに同じファイルを使った")
        self.assertEqual(io.open(path_a, "rb").read(), BODY_A,
                         "先にダウンロードした版の中身が入れ替わっている")
        self.assertEqual(io.open(path_b, "rb").read(), BODY_B)

    def test_two_downloads_at_once_both_succeed(self):
        """同時に受信しても、両方とも残ること。"""
        from core.version_manager import VersionManager

        results = {}
        mgrs = []
        for _ in range(2):
            mgr = VersionManager()
            mgr.UPDATE_DIR = self.tmp
            mgrs.append(mgr)

        with unittest.mock.patch("core.version_manager.requests.get",
                                 side_effect=_fake_get(slow=True)):
            threads = [
                threading.Thread(target=self._download,
                                 args=(URL_A, "9.9.1", mgrs[0], results, "A")),
                threading.Thread(target=self._download,
                                 args=(URL_B, "9.9.2", mgrs[1], results, "B")),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)

        self.assertIsNotNone(results.get("A"), "同時受信で A が失敗した")
        self.assertIsNotNone(results.get("B"), "同時受信で B が失敗した")
        self.assertEqual(io.open(results["A"], "rb").read(), BODY_A)
        self.assertEqual(io.open(results["B"], "rb").read(), BODY_B)
        self.assertEqual([n for n in os.listdir(self.tmp)
                          if n.endswith(".part")], [],
                         "書きかけが残っている")


class ApplyChecksTheShownVersionTest(unittest.TestCase):
    """適用の直前に、表示していた版と同じ ZIP かを確かめること。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="netbelt-apply-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.popen = unittest.mock.MagicMock()
        self.warned = []
        for p in (unittest.mock.patch("subprocess.Popen", self.popen),
                  unittest.mock.patch(
                      "PyQt6.QtWidgets.QMessageBox.warning",
                      lambda *a, **k: self.warned.append(a[2])),
                  unittest.mock.patch(
                      "PyQt6.QtWidgets.QMessageBox.critical",
                      lambda *a, **k: self.warned.append(a[2]))):
            p.start()
            self.addCleanup(p.stop)

    def _place_zip(self, version, body=BODY_A):
        path = os.path.join(self.tmp, "NetBelt-%s.zip" % version)
        io.open(path, "wb").write(body)
        io.open(path + ".sha256", "w").write(hashlib.sha256(body).hexdigest())
        io.open(path + ".version", "w").write(version)
        return path

    def _apply(self, shown_version, zip_path):
        from ui.dialogs.update_dialog import UpdateDialog
        dialog = UpdateDialog(None, {"version": shown_version})
        self.addCleanup(dialog.deleteLater)
        dialog.downloaded_zip_path = zip_path
        # 凍結ビルドのつもりで走らせる。updater.bat は venv 側を指すので、
        # 存在確認だけ通す（ZIP と控えは本物を置いてある）。
        with unittest.mock.patch.object(sys, "frozen", True, create=True), \
                unittest.mock.patch("os.path.exists", return_value=True):
            dialog._on_apply_clicked()

    def test_a_zip_from_another_version_is_refused(self):
        path = self._place_zip("9.9.2", BODY_B)
        self._apply("9.9.1", path)

        self.assertEqual(self.popen.call_count, 0,
                         "表示と違う版の ZIP を updater.bat へ渡した")
        self.assertTrue(self.warned, "黙って諦めている")

    def test_a_tampered_zip_is_refused(self):
        path = self._place_zip("9.9.1")
        with io.open(path, "ab") as f:
            f.write(b"TAMPERED")
        self._apply("9.9.1", path)

        self.assertEqual(self.popen.call_count, 0,
                         "検証を通らない ZIP を updater.bat へ渡した")
        self.assertTrue(self.warned, "黙って諦めている")

    def test_the_matching_zip_is_applied(self):
        path = self._place_zip("9.9.1")
        self._apply("9.9.1", path)

        self.assertEqual(self.popen.call_count, 1,
                         "正しい ZIP まで当てられなくなっている: %s" % self.warned)


if __name__ == "__main__":
    unittest.main()
