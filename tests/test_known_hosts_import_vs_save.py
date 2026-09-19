"""旧 known_hosts の引き継ぎと、ホスト鍵の保存・読み込みが互いを壊さないことを検証する。

実測で起きていたこと（旧 ~/.terminal-tool/known_hosts があり、引き継ぎの
目印がまだ無い初回に、2 接続以上を同時に始めたときだけ起きる）:

1. 引き継ぎ（_import_legacy_known_hosts）は新しい known_hosts を読み、
   旧い行を足して os.replace で丸ごと差し替える。ところが保存
   （_save_known_hosts）の錠を使っていなかった。B の引き継ぎが
   known_hosts を読んだあとに A が TOFU で鍵を保存し、そのあと B が
   読んだ時点の内容で差し替えると、A の鍵が消える。localhost の
   paramiko サーバ 2 台でこの順を強制すると、次に A の鍵を差し替えて
   繋ぎ直したとき、変わった鍵が確認なしで受け入れられ、パスワードまで
   送られた。
2. 自然に起きたのは別の形で、片方の os.replace と他方の load_host_keys が
   Windows で衝突し、「既知ホスト鍵 (known_hosts) を読めないため接続を
   中止しました: [Errno 13] Permission denied」で接続が中止された
   （30 回中 5 回、40 回中 1 回）。

直し方: 保存の錠を config_manager へ移し、引き継ぎ（読み取り→差し替え→
目印作成）と、_setup_host_keys の load_host_keys も同じ錠の中で行う。
"""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

import paramiko                                     # noqa: E402

OLD_HOST = "192.0.2.1"
NEW_HOST = "192.0.2.2"


def _entry(host, key):
    return "%s %s %s\n" % (host, key.get_name(), key.get_base64())


class KnownHostsImportVsSaveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="netbelt-khrace-"))
        home = mock.patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        self.known_hosts = self.home / ".netbelt" / "known_hosts"

    def _run(self, target):
        """target を別スレッドで走らせる。終わったら done が立つ。"""
        done = threading.Event()

        def body():
            try:
                target()
            finally:
                done.set()

        thread = threading.Thread(target=body, daemon=True)
        return thread, done

    def test_a_key_saved_while_the_import_is_writing_survives(self):
        """引き継ぎの差し替えが、その間に TOFU で保存された鍵を消さないこと。"""
        from core import config_manager
        from core.ssh_connection import _TofuHostKeyPolicy

        old_dir = self.home / ".terminal-tool"
        old_dir.mkdir()
        (old_dir / "known_hosts").write_text(
            _entry(OLD_HOST, paramiko.ECDSAKey.generate()), encoding="utf-8")

        # B の引き継ぎを、known_hosts を読み終えて書き始める所で止める
        real_mkstemp = tempfile.mkstemp
        paused = threading.Event()
        resume = threading.Event()
        self.addCleanup(resume.set)
        importer = {}

        def gated_mkstemp(*args, **kwargs):
            if threading.current_thread() is importer.get("thread"):
                paused.set()
                resume.wait(5.0)
            return real_mkstemp(*args, **kwargs)

        with mock.patch("core.config_manager.tempfile.mkstemp", gated_mkstemp):
            thread_b, _ = self._run(config_manager.app_data_dir)
            importer["thread"] = thread_b
            thread_b.start()
            self.assertTrue(paused.wait(5.0), "前提: 引き継ぎが書き込みまで進んでいない")

            # その間に A が初めての機器の鍵を TOFU で保存する
            client_a = paramiko.SSHClient()
            policy_a = _TofuHostKeyPolicy(self.known_hosts)
            thread_a, saved_a = self._run(lambda: policy_a.missing_host_key(
                client_a, NEW_HOST, paramiko.ECDSAKey.generate()))
            thread_a.start()
            saved_during_import = saved_a.wait(0.5)

            resume.set()
            thread_b.join(5.0)
            thread_a.join(5.0)
        self.assertFalse(thread_b.is_alive(), "引き継ぎが戻ってこない")
        self.assertFalse(thread_a.is_alive(), "保存が戻ってこない")
        config_manager.take_known_hosts_import_warning()

        saved = paramiko.HostKeys(str(self.known_hosts))
        self.assertIsNotNone(
            saved.lookup(NEW_HOST),
            "引き継ぎが読んだ時点の内容で差し替え、その間に保存された鍵を消した: %r"
            % self.known_hosts.read_text(encoding="utf-8"))
        self.assertIsNotNone(saved.lookup(OLD_HOST), "旧 known_hosts の鍵が引き継がれていない")
        self.assertFalse(saved_during_import,
                         "引き継ぎが差し替えている最中に、保存が割り込んでいる")

    def test_loading_waits_for_a_save_that_is_replacing_the_file(self):
        """保存が差し替えている最中の known_hosts を、接続が読みにいかないこと。

        Windows では、差し替えの最中に開くと Permission denied になり、
        _setup_host_keys が接続を中止する。
        """
        from core.ssh_connection import SSHConnection, _save_known_hosts

        self.known_hosts.parent.mkdir(parents=True)
        self.known_hosts.write_text(
            _entry(OLD_HOST, paramiko.ECDSAKey.generate()), encoding="utf-8")

        # S は鍵を足して保存する。書き終える前で止める
        client_s = paramiko.SSHClient()
        key = paramiko.ECDSAKey.generate()
        client_s.get_host_keys().add(NEW_HOST, key.get_name(), key)
        real_save = client_s.save_host_keys
        saving = threading.Event()
        resume = threading.Event()
        self.addCleanup(resume.set)

        def paused_save(filename):
            saving.set()
            resume.wait(5.0)
            real_save(filename)

        client_s.save_host_keys = paused_save
        thread_s, _ = self._run(lambda: _save_known_hosts(client_s, self.known_hosts))
        thread_s.start()
        self.assertTrue(saving.wait(5.0), "前提: 保存が書き込みまで進んでいない")

        # L は既知ホスト鍵を読み込んで接続の準備をする
        conn = SSHConnection(NEW_HOST, 22, "admin", password="pw")
        client_l = paramiko.SSHClient()
        real_load = client_l.load_host_keys
        loaded_while_saving = []

        def watched_load(filename):
            loaded_while_saving.append(not resume.is_set())
            real_load(filename)

        client_l.load_host_keys = watched_load
        # 引き継ぎは済んでいる状態。読み込みそのものが待つかだけを見る
        with mock.patch("core.config_manager.app_data_dir",
                        return_value=self.known_hosts.parent):
            thread_l, _ = self._run(lambda: conn._setup_host_keys(client_l))
            thread_l.start()
            thread_l.join(0.5)

            resume.set()
            thread_s.join(5.0)
            thread_l.join(5.0)
        self.assertFalse(thread_s.is_alive(), "保存が戻ってこない")
        self.assertFalse(thread_l.is_alive(), "読み込みが戻ってこない")

        self.assertEqual(loaded_while_saving, [False],
                         "保存が known_hosts を差し替えている最中に読み込んでいる"
                         "（Windows では Permission denied で接続が中止される）")
        self.assertIsNotNone(client_l.get_host_keys().lookup(NEW_HOST),
                             "差し替え後の known_hosts を読んでいない")


if __name__ == "__main__":
    unittest.main()
