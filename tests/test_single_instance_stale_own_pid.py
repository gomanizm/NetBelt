# -*- coding: utf-8 -*-
"""異常終了で残った錠の PID を次の起動が引き継いでも、起動を断らないことを確認する。

何が起きていたか: QLockFile は錠に書かれた PID のプロセスが同じ実行体名で
生きていれば「相手が持っている」と判定する。Windows は解放した PID をすぐ
再利用するので、異常終了で残った錠と同じ PID を得た次の起動は、自分自身を
錠の持ち主と読み、誰も待っていない名前へ数秒繋ぎに行ってから黙って終わって
いた。錠を本物の QLockFile に書かせ、解放してから同じ中身を置き直して
（PID は自分のもの）再現する。
"""
import os
import sys
import unittest
import uuid

sys.path.insert(0, "src")


class StaleLockWithOwnPidTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _leave_lock_with_own_pid(self, name):
        """自分の PID が書かれた錠ファイルを残す（異常終了の模擬）"""
        from PyQt6.QtCore import QLockFile
        from core.single_instance import default_lock_path
        path = default_lock_path(name)
        lock = QLockFile(path)
        lock.setStaleLockTime(0)
        self.assertTrue(lock.tryLock(0))
        with open(path, "rb") as f:
            content = f.read()
        lock.unlock()
        with open(path, "wb") as f:
            f.write(content)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _guard(self, name):
        from core.single_instance import SingleInstanceGuard
        guard = SingleInstanceGuard(name=name)
        guard.RAISE_RETRY_MS = 200
        self.addCleanup(guard.close)
        return guard

    def test_a_leftover_lock_with_our_own_pid_does_not_refuse_the_start(self):
        """自分の PID の残った錠では断られず、錠を取り直せること。"""
        name = "netbelt-test-%s" % uuid.uuid4().hex
        self._leave_lock_with_own_pid(name)
        guard = self._guard(name)

        self.assertFalse(guard.another_instance_is_running(),
                         "自分の PID の残った錠で起動を断っている")
        self.assertIsNotNone(guard._lock, "錠を取り直せていない")

    def test_a_lock_held_by_another_guard_in_this_process_still_refuses(self):
        """同じプロセスの中で実際に持っている錠は、古い錠として外さないこと。"""
        name = "netbelt-test-%s" % uuid.uuid4().hex
        first = self._guard(name)
        self.assertFalse(first.another_instance_is_running())
        second = self._guard(name)

        self.assertTrue(second.another_instance_is_running(),
                        "持っている錠を外して 2 つ目を通している")
        self.assertIsNone(second._lock)


if __name__ == "__main__":
    unittest.main()
