"""NetBelt を 2 つ起動しても、known_hosts の保存と読み込みがぶつからないこと。

実測（8b0c94e、同じ HOME で保存側と読み込み側を別プロセスで同時に走らせた）:
保存 300 回のうち 229 回が os.replace の PermissionError [WinError 5] になり、
TOFU の保存が『known_hosts を保存できません…』の警告に落ちて、その機器の
鍵が残らなかった。次に繋ぐと同じ機器がまた「未知」になり、鍵が変わっていても
確認なしで受け入れられる。読み込み側も 3000 回のうち 6 回が『既知ホスト鍵
(known_hosts) を読めないため接続を中止しました: [Errno 13] Permission
denied』で接続を落とした。排他が threading.Lock（known_hosts_lock）だけで、
同じプロセスの中しか守っていなかったため。

直し方: known_hosts の隣にロック用ファイルを置き、標準ライブラリだけで
（Windows は msvcrt.locking）別プロセスとも排他する。同じプロセスの中の
threading.Lock はそのまま残す。取れないまま長く待たせないよう数秒で
見切り、そのときは今までどおりの警告・中止の文言にする。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, "src")

REPO_ROOT = Path(__file__).resolve().parent.parent
SAVES = 100
LOADS = 1000


def _worker(home, role, count):
    """別プロセス側の本体。保存か読み込みを count 回くり返して結果を出す。"""
    from unittest import mock
    home = Path(home)
    mock.patch.object(Path, "home", return_value=home).start()

    import paramiko
    from core.ssh_connection import (SSHConnection, HostKeyStoreError,
                                     _save_known_hosts)

    known_hosts = home / ".netbelt" / "known_hosts"
    while not (home / "go").exists():
        time.sleep(0.01)

    failures = 0
    reasons = {}
    key = paramiko.ECDSAKey.generate()
    for i in range(int(count)):
        try:
            if role == "saver":
                client = paramiko.SSHClient()
                client.get_host_keys().add(
                    "s%d.example.com" % i, key.get_name(), key)
                _save_known_hosts(client, known_hosts)
            else:
                connection = SSHConnection("192.0.2.1", 22, "admin",
                                           password="pw")
                connection._setup_host_keys(paramiko.SSHClient())
        except (HostKeyStoreError, OSError, Exception) as error:
            failures += 1
            reason = "%s: %s" % (type(error).__name__, str(error)[:70])
            reasons[reason] = reasons.get(reason, 0) + 1
    print("RESULT " + json.dumps(
        {"role": role, "failures": failures, "reasons": reasons}))


class KnownHostsCrossProcessLockTest(unittest.TestCase):
    """NetBelt を 2 つ起動した状態（同じ HOME の 2 プロセス）を作って数える。"""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="netbelt-khxproc-"))
        (self.home / ".netbelt").mkdir()

    def _spawn(self, role, count):
        env = dict(os.environ, PYTHONIOENCODING="utf-8",
                   QT_QPA_PLATFORM="offscreen")
        return subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker",
             str(self.home), role, str(count)],
            cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace")

    @staticmethod
    def _result(process):
        out, _ = process.communicate(timeout=600)
        for line in out.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        raise AssertionError("worker が結果を返していない:\n%s" % out[-2000:])

    def test_two_processes_do_not_break_each_others_known_hosts(self):
        processes = [self._spawn("saver", SAVES), self._spawn("loader", LOADS)]
        try:
            # 両方が paramiko の読み込みを終えて待ち合わせに入るのを待つ
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if all(p.poll() is None for p in processes):
                    time.sleep(2.0)
                    break
                time.sleep(0.1)
            (self.home / "go").write_text("", encoding="utf-8")
            results = [self._result(p) for p in processes]
        finally:
            for p in processes:
                if p.poll() is None:
                    p.kill()

        saver, loader = results
        self.assertEqual(
            saver["failures"], 0,
            "保存が %d/%d 回失敗した（その機器の鍵が残らない）: %r"
            % (saver["failures"], SAVES, saver["reasons"]))
        self.assertEqual(
            loader["failures"], 0,
            "読み込みが %d/%d 回、接続を中止した: %r"
            % (loader["failures"], LOADS, loader["reasons"]))

        import paramiko
        saved = paramiko.HostKeys(str(self.home / ".netbelt" / "known_hosts"))
        self.assertEqual(
            len(saved), SAVES,
            "保存できたはずの鍵が known_hosts に残っていない: %d/%d"
            % (len(saved), SAVES))


class KnownHostsLockTimeoutTest(unittest.TestCase):
    """ロックを取れないまま待たせ続けないこと（数秒で見切る）。"""

    def setUp(self):
        from unittest import mock
        self.home = Path(tempfile.mkdtemp(prefix="netbelt-khtimeout-"))
        self.known_hosts = self.home / ".netbelt" / "known_hosts"
        self.known_hosts.parent.mkdir(parents=True)
        # ほかの NetBelt がロックを離さない状態
        stuck = mock.patch("core.config_manager._try_known_hosts_lock",
                           return_value=False)
        stuck.start()
        self.addCleanup(stuck.stop)
        short = mock.patch("core.config_manager.KNOWN_HOSTS_LOCK_TIMEOUT", 0.2)
        short.start()
        self.addCleanup(short.stop)

    def test_loading_aborts_the_connection_with_the_usual_wording(self):
        from unittest import mock
        import paramiko
        from core.ssh_connection import SSHConnection, HostKeyStoreError
        connection = SSHConnection("192.0.2.1", 22, "admin", password="pw")
        started = time.monotonic()
        with mock.patch("core.config_manager.app_data_dir",
                        return_value=self.known_hosts.parent):
            with self.assertRaises(HostKeyStoreError) as caught:
                connection._setup_host_keys(paramiko.SSHClient())
        self.assertLess(time.monotonic() - started, 5.0, "待ちすぎている")
        self.assertIn("接続を中止しました", str(caught.exception))

    def test_saving_warns_instead_of_dropping_the_key_silently(self):
        import paramiko
        from core.ssh_connection import _TofuHostKeyPolicy
        policy = _TofuHostKeyPolicy(self.known_hosts)
        shown = []
        policy._on_save_error = shown.append
        client = paramiko.SSHClient()
        started = time.monotonic()
        policy.missing_host_key(client, "192.0.2.2",
                                paramiko.ECDSAKey.generate())
        self.assertLess(time.monotonic() - started, 5.0, "待ちすぎている")
        self.assertEqual(len(shown), 1, "保存できないことを伝えていない")
        self.assertIn("known_hosts を保存できません", shown[0])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        _worker(*sys.argv[2:])
    else:
        unittest.main()
