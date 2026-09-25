"""ほぼ同時に 2 つ起動しても、開くのは 1 つだけであることを検証する。

実測（f4cad23）: main() は「存在確認 → listen」の 2 操作で多重起動を断って
いた（src/main.py:96-98）。この 2 つの間に相手が入り込めるので、2 プロセスが
どちらも相手の待受開始より前に存在確認を終えると、両方が MainWindow を開く。
このテストの待ち合わせ付き 2 プロセス起動では 3/3 で両方の窓が開き、検査役の
実測では待ち合わせ無しの同時起動でも 30/30 だった。競合が成立する幅は
約 3 ミリ秒（検査役の実測。B を 50 ミリ秒ずらすと 0/3）で、.bat の start 2 回・
スタートアップ登録と手動起動の重なり・ショートカット 2 つを選んで Enter の
ような「ほぼ同時起動」では確実に起きる。

そうなると設定が消える。同じ config.json を見る ConfigManager を 2 つ作り、
A が add_group してから B が set_last_check_time（更新チェック時刻という
何気ない保存）を 1 回呼ぶと、ディスク上のグループは
['Default', '本番環境', '検証環境', '追加グループ'] から
['Default', '本番環境', '検証環境'] へ戻り、A が追加したグループが警告も
バックアップも無しに消えた（src/core/config_manager.py:1037 save_config() は
self.config を丸ごと書き戻すだけで、読み込み以降のディスクの更新を見ない）。

既存の tests/test_single_instance.py は、先行側が listen() を終えたあとの
逐次起動しか見ていないので、この競合を守っていなかった。

直し方: 存在確認そのものを不可分な取得にする。QLocalServer.listen() は
Windows では排他にならない（同じ名前で 2 つ目の名前付きパイプが作れてしまう
ことを実測した）ので、錠には QLockFile を使う（PyQt6.QtCore なので依存は
足さない）。錠を取れた方だけが本体になり、取れなかった方は起動せず、
動いている方の窓を前へ出してもらってから終わる。相手がまだ listen() まで
来ていないことがあるので、合図は少しの間くり返す（1 回きりだと「2 回目の
ダブルクリックで何も起きない」が起きうる）。異常終了で残った錠は、持ち主の
プロセスが居なくなった時点で取り直せる（実測: 保持プロセスを kill した
あとの tryLock(0) は True）。錠を作れない環境では、今までどおり断らずに
そのまま起動する。錠が取れたときも名前へ繋がるかは今までどおり見る。
錠を知らない版（この変更より前の NetBelt）が動いていると錠は空いた
ままなので、錠だけで決めるとその版とは 2 つ開いてしまう。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

REPO_ROOT = Path(__file__).resolve().parent.parent
# 子プロセスが待ち合わせに使える上限（秒）。CPU が混んでいても
# PyQt6 の読み込みが終わるまで待てる長さにする
BARRIER_TIMEOUT = 90


def _barrier(workdir, label, expected=2):
    """2 つの子プロセスを同じ瞬間へ揃える（揃ったら返る）

    競合が成立する幅は約 3 ミリ秒しかないので、ファイルを見つけた順に
    進むと、見つけるのが数ミリ秒遅れた方はもう競合に間に合わない。
    全員そろった時点で「動き出す時刻」を決め、そこまでは空回りで待つ。
    """
    Path(workdir, "%s.ready" % label).write_text("1", encoding="utf-8")
    go = Path(workdir, "go")
    deadline = time.monotonic() + BARRIER_TIMEOUT
    while not go.exists():
        if time.monotonic() > deadline:
            raise AssertionError("待ち合わせが揃わなかった")
        if len(list(Path(workdir).glob("*.ready"))) >= expected:
            go.write_text(repr(time.time() + 0.2), encoding="utf-8")
            break
        time.sleep(0.002)
    start = None
    while start is None:
        try:
            # 書き終わる前に読むと空文字になる
            start = float(go.read_text(encoding="utf-8"))
        except ValueError:
            time.sleep(0.001)
    while time.time() < start:
        pass


def _worker(mode, label, workdir, name, listen_delay):
    """別プロセス側の本体。main() と同じ順序で起動を試みる。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from core.single_instance import SingleInstanceGuard

    guard = SingleInstanceGuard(name=name)
    if mode == "hold":
        # 錠を取ったまま外から強制終了される側（異常終了の再現）
        guard.another_instance_is_running()
        guard.listen()
        print("READY", flush=True)
        while True:
            app.processEvents()
            time.sleep(0.05)

    raised = []
    guard.raise_window = lambda: raised.append(True)

    _barrier(workdir, label)
    # --- main() の存在確認 ---
    if guard.another_instance_is_running():
        print("RESULT " + json.dumps({"label": label, "opened": False}),
              flush=True)
        return
    # MainWindow を作っている最中（設定ファイルの警告など）で待受が遅れる分
    time.sleep(float(listen_delay))
    # --- main() の listen() ---
    guard.listen()
    # --- main() の MainWindow(): ここで窓が開く ---
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and not raised:
        app.processEvents()
        time.sleep(0.01)
    print("RESULT " + json.dumps(
        {"label": label, "opened": True, "raised": bool(raised)}), flush=True)
    guard.close()


class ConcurrentStartTest(unittest.TestCase):
    """ほぼ同時に起動した 2 プロセスを実際に作って数える。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="netbelt-race-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.names = []
        self.addCleanup(self._remove_lock_files)

    def _remove_lock_files(self):
        """強制終了した子プロセスが残した錠ファイルを片付ける。"""
        try:
            from core.single_instance import default_lock_path
        except ImportError:      # 錠を入れる前の版で走らせたとき
            return
        for name in self.names:
            path = default_lock_path(name)
            if os.path.exists(path):
                os.remove(path)

    def _new_run(self):
        """1 回分の待ち合わせ用ディレクトリと名前を用意する。"""
        name = "netbelt-test-%s" % uuid.uuid4().hex
        self.names.append(name)
        workdir = os.path.join(self.root, name)
        os.mkdir(workdir)
        return workdir, name

    def _spawn(self, mode, label, workdir, name, listen_delay=0.0):
        env = dict(os.environ, PYTHONIOENCODING="utf-8",
                   QT_QPA_PLATFORM="offscreen")
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", mode,
             label, workdir, name, str(listen_delay)],
            cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace")
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process

    @staticmethod
    def _result(process):
        out, _ = process.communicate(timeout=300)
        for line in out.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        raise AssertionError("起動の結果が返っていない:\n%s" % out[-2000:])

    def _start_two(self, listen_delay=0.0):
        workdir, name = self._new_run()
        processes = [self._spawn("start", label, workdir, name, listen_delay)
                     for label in ("A", "B")]
        return [self._result(p) for p in processes]

    def test_two_simultaneous_starts_open_only_one_window(self):
        """同時に起動しても、窓が開くのは 1 つだけであること。

        競合が成立する幅は約 3 ミリ秒しかなく、待ち合わせがすり抜けて
        逐次起動になることがあるので、2 回くり返す。
        """
        for trial in range(2):
            results = self._start_two()

            opened = [r["label"] for r in results if r["opened"]]
            self.assertEqual(
                len(opened), 1,
                "同時起動の %d 回目に %d つの窓が開いた（両方が設定を丸ごと"
                "書き戻すので、あとから保存した方が相手の機器・パスワード・"
                "グループ・マクロを消す）: %r"
                % (trial + 1, len(opened), results))

    def test_the_refused_start_wakes_the_running_window(self):
        """断られた方が、動いている方の窓を前へ出させること。

        勝った方が listen() へ着く前に断られることがあるので、合図が
        1 回きりだと「2 回目のダブルクリックで何も起きない」になる。
        """
        results = self._start_two(listen_delay=0.6)

        opened = [r for r in results if r["opened"]]
        self.assertEqual(len(opened), 1,
                         "窓が開いた数が 1 ではない: %r" % (results,))
        self.assertTrue(
            opened[0]["raised"],
            "断られた方の合図が届かず、動いている方の窓が前へ出ていない:"
            " %r" % (results,))

    def test_a_lock_left_by_a_crash_does_not_block_the_next_start(self):
        """異常終了で残った錠で、次の起動ができなくならないこと。

        錠は残ったままになるが、持ち主のプロセスが居なくなっていれば
        取り直せる。名前付きパイプが OS から消えるのは強制終了の
        少しあと（実測 20 ミリ秒ほど）なので、そのぶんは見直す。
        """
        from core.single_instance import SingleInstanceGuard
        workdir, name = self._new_run()
        holder = self._spawn("hold", "H", workdir, name)
        self.assertEqual(holder.stdout.readline().strip(), "READY",
                         "錠を持つ側が起動できていない")
        holder.kill()
        holder.wait(timeout=60)

        deadline = time.monotonic() + 10
        while True:
            guard = SingleInstanceGuard(name=name)
            self.addCleanup(guard.close)
            if not guard.another_instance_is_running():
                break
            guard.close()
            if time.monotonic() > deadline:
                self.fail("強制終了したプロセスの印で起動できなくなっている")
            time.sleep(0.05)


class SingleInstanceLockTest(unittest.TestCase):
    """錠そのものの振る舞い（同一プロセス内で確かめられる分）。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _guard(self, name=None):
        from core.single_instance import SingleInstanceGuard
        guard = SingleInstanceGuard(
            name=name or "netbelt-test-%s" % uuid.uuid4().hex)
        # 待受が始まらない相手を待ち続けないよう、合図の再試行は短くする
        guard.RAISE_RETRY_MS = 50
        self.addCleanup(guard.close)
        return guard

    def test_a_start_is_refused_before_the_first_one_listens(self):
        """先に起動した方が待受を始める前でも、2 つ目を断ること。

        MainWindow を作っている間は listen() より前なので、そこが競合の
        窓になっていた。
        """
        first = self._guard()
        self.assertFalse(first.another_instance_is_running(),
                         "前提: 1 つ目は断られない")
        second = self._guard(name=first._name)

        self.assertTrue(
            second.another_instance_is_running(),
            "1 つ目が listen() へ着く前だと 2 つ目も起動してしまう")

    def test_a_start_is_refused_by_a_version_that_does_not_take_the_lock(self):
        """錠を知らない版が動いていても、2 つ目を断ること。

        この変更より前の NetBelt は名前（QLocalServer）だけで断っていた。
        その版が動いているときは錠が空いているので、錠が取れただけで
        起動してよいことにはならない。
        """
        from PyQt6.QtNetwork import QLocalServer
        guard = self._guard()
        old_version = QLocalServer()
        self.addCleanup(old_version.close)
        self.assertTrue(old_version.listen(guard._name),
                        "前提: 錠を知らない版が名前を取れる")

        self.assertTrue(guard.another_instance_is_running(),
                        "錠を知らない版が動いているのに起動してしまう")

    def test_a_start_is_not_refused_when_the_lock_cannot_be_used(self):
        """錠を作れない環境では、断らずにそのまま起動すること。"""
        from PyQt6.QtCore import QLockFile
        guard = self._guard()

        with mock.patch.object(QLockFile, "tryLock", return_value=False), \
             mock.patch.object(QLockFile, "error",
                               return_value=QLockFile.LockError.UnknownError):
            refused = guard.another_instance_is_running()

        self.assertFalse(refused, "錠を作れないだけで起動を断っている")

    def test_closing_releases_the_lock(self):
        """終了したら錠も手放し、次の起動が断られないこと。"""
        first = self._guard()
        self.assertFalse(first.another_instance_is_running())
        first.close()

        second = self._guard(name=first._name)
        self.assertFalse(second.another_instance_is_running(),
                         "終了したのに錠が残っている")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        _worker(*sys.argv[2:])
    else:
        unittest.main()
