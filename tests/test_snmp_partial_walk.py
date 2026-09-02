"""WALK が途中で途切れても、取れた分を捨てないことを検証する。

_perform_walk は errorIndication を見つけると即 raise していた。例外は
SNMPWorker.run() の except で捕まって result_ready(False, ...) になるため、
それまで results に貯めた行は誰にも渡らず捨てられる。パネル側は
success=False のときテーブルを更新しないので、利用者にはエラーだけが出て、
何行取れていたのかも分からない。

しかも UdpTransportTarget にタイムアウトを渡していないので既定値
（timeout=1 秒 / retries=5）が効き、1リクエストあたり最大6秒。WALK は
OID のステップごとに1リクエストなので、ifTable のように何百ステップも
回るものほど、途中で1回応答を落とす確率が上がって成果がゼロになる。

取れた分は警告付きで見せる。1件も取れていないなら、これまでどおりエラー。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

OK_ROWS = [
    ("1.3.6.1.2.1.1.1.0", "OctetString", "Cisco IOS Software"),
    ("1.3.6.1.2.1.1.3.0", "TimeTicks", "123456"),
    ("1.3.6.1.2.1.1.5.0", "OctetString", "router01"),
]


class _VarBind:
    """pysnmp の varBind を模す（prettyPrint を持つ組）。"""

    def __init__(self, oid, value, type_name):
        self._oid = _Pretty(oid)
        self._value = _Pretty(value, type_name)

    def __getitem__(self, index):
        return self._oid if index == 0 else self._value


class _Pretty:
    def __init__(self, text, type_name=None):
        self._text = text
        if type_name:
            self.__class__ = type(type_name, (_Pretty,), {})
            self._text = text

    def prettyPrint(self):
        return self._text


def _rows_then(error, rows=OK_ROWS):
    """rows を返したあと error を出す nextCmd の代わり。"""
    def fake(*args, **kwargs):
        for oid, type_name, value in rows:
            vb = _VarBind(oid, value, type_name)
            yield (None, None, None, [vb])
        if error is not None:
            yield (error, None, None, [])
    return fake


class SnmpPartialWalkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _run_walk(self, fake_next_cmd):
        """WALK を1回まわし、(成否, 結果, 警告) を返す。"""
        from core.snmp_manager import SNMPWorker
        worker = SNMPWorker("walk", {
            "host": "192.0.2.1", "port": 161, "oid": "1.3.6.1.2.1.1",
            "version": "v2c", "community": "public",
        })
        seen = []
        warnings = []
        worker.result_ready.connect(lambda ok, r: seen.append((ok, r)))
        if hasattr(worker, "partial_result"):
            worker.partial_result.connect(warnings.append)

        with mock.patch("core.snmp_manager.nextCmd", fake_next_cmd):
            worker.run()

        self.assertTrue(seen, "結果が1度も出ていない")
        ok, result = seen[-1]
        return ok, result, warnings

    def test_a_walk_interrupted_partway_keeps_what_it_collected(self):
        """途中で途切れても、それまでの行を渡すこと。"""
        ok, result, _ = self._run_walk(_rows_then("requestTimedOut"))

        self.assertTrue(ok, "取れた行があるのにエラー扱いにしている")
        self.assertEqual(len(result), len(OK_ROWS),
                         "取得済みの行が捨てられている")

    def test_an_interrupted_walk_is_reported_as_incomplete(self):
        """不完全であることを伝えること（黙って部分結果を出さない）。"""
        _, _, warnings = self._run_walk(_rows_then("requestTimedOut"))

        self.assertTrue(warnings, "途中で切れたことがどこにも出ていない")
        self.assertIn("requestTimedOut", " ".join(warnings),
                      "何が起きたのか分からない文面: %s" % warnings)

    def test_a_walk_that_fails_before_anything_is_still_an_error(self):
        """1件も取れていないなら、これまでどおりエラーにすること。"""
        ok, result, _ = self._run_walk(
            _rows_then("noSuchName", rows=[]))

        self.assertFalse(ok, "何も取れていないのに成功扱いにしている")

    def test_a_complete_walk_is_not_flagged_as_incomplete(self):
        """最後まで回れたら、警告を出さないこと。"""
        ok, result, warnings = self._run_walk(_rows_then(None))

        self.assertTrue(ok)
        self.assertEqual(len(result), len(OK_ROWS))
        self.assertEqual(warnings, [], "完走したのに警告が出ている")


class PartialWalkReachesTheUserTest(unittest.TestCase):
    """警告がパネルまで届くこと（信号を足しただけでは直っていない）。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self):
        from ui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.close)
        return window.snmp_panel

    def test_the_panel_shows_the_partial_results(self):
        """途中で切れても、取れた行が表に出ること。"""
        panel = self._panel()
        panel._on_operation_partial("requestTimedOut")
        panel._on_operation_completed(True, list(OK_ROWS))

        self.assertEqual(panel.result_model.rowCount(), len(OK_ROWS),
                         "取れた行が表に出ていない")

    def test_the_panel_says_the_results_are_incomplete(self):
        """全部ではないことが読み取れること。"""
        panel = self._panel()
        panel._on_operation_partial("requestTimedOut")
        panel._on_operation_completed(True, list(OK_ROWS))

        shown = panel.status_label.text()
        self.assertIn("途中", shown,
                      "不完全であることが表示から分からない: %r" % shown)

    def test_a_complete_walk_is_shown_as_complete(self):
        """完走したときに、余計な警告を残さないこと。"""
        panel = self._panel()
        panel._on_operation_partial("requestTimedOut")
        panel._on_operation_completed(True, list(OK_ROWS))
        # 続けてもう一度、今度は完走させる
        panel._on_operation_completed(True, list(OK_ROWS))

        shown = panel.status_label.text()
        self.assertNotIn("途中", shown,
                         "前回の警告が残っている: %r" % shown)

    def test_the_manager_passes_the_warning_on(self):
        """マネージャがワーカーの警告を中継していること。"""
        from core.snmp_manager import SNMPManager
        manager = SNMPManager()
        self.assertTrue(hasattr(manager, "operation_partial"),
                        "中継用のシグナルが無い")


if __name__ == "__main__":
    unittest.main()
