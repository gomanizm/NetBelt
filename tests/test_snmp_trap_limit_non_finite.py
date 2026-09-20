"""保持上限が非有限数でも、アプリが起動できることを検証する。

config.json は手で編集できる。settings.snmp.max_traps に JSON の数値
として 1e309 や Infinity と書くと、json.load はこれを float の無限大と
して読む。_configured_max_traps の int() はそこで OverflowError を送出
するが、捕捉していたのは (AttributeError, TypeError, ValueError) だけ
だった。OverflowError は ArithmeticError 系なのでこの網から漏れる。

実測（一時フォルダの config.json に
`{"groups": [], "settings": {"snmp": {"max_traps": 1e309}}}` を書いて
MainWindow を作る）:

    loaded max_traps = inf
    File "src\\ui\\main_window.py", line 392, in _create_main_widget
        self.snmp_panel = SNMPPanel(config_manager=self.config_manager)
    File "src\\ui\\snmp_panel.py", line 182, in __init__
        self.max_traps = self._configured_max_traps()
    File "src\\ui\\snmp_panel.py", line 807, in _configured_max_traps
        value = int(settings.get("snmp", {}).get(
    OverflowError: cannot convert float infinity to integer

例外は SNMPPanel のコンストラクタから MainWindow の初期化まで抜ける。
main.py の MainWindow() は try で囲っていないので、画面には何も出ずに
ログだけ残して終了する。値は config.json に残るため、その config.json を
消すか直すまで毎回起動に失敗する。

なお NaN は int(nan) が ValueError なので、これまでも既定値へ落ちて
いた。危ないのは非有限のうち ±inf だけ。

直し方: 捕捉に OverflowError を足し、さらに上限として使う前に
math.isfinite で足切りする（`value > 0` は inf でも真になるため、
捕捉だけでは将来 int() を通る経路が増えたときに素通りする）。
"""
import json
import math
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SnmpTrapLimitNonFiniteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 作ったウィンドウはクラス終了まで保持する。パネルはウィンドウの子なので
    # ウィンドウを先に捨てると C++ 側ごと消えて触れなくなる
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _panel_from_json(self, literal):
        """max_traps に literal を書いた config.json から MainWindow を作る

        手で編集された config.json をそのまま読ませたいので、dict を
        差し込まずに JSON のテキストから読み込ませる。
        """
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-trap-nonfinite-")
        path = os.path.join(d, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"groups": [], "settings": {"snmp": {"max_traps": %s}}}'
                    % literal)
        cm = ConfigManager(config_path=path)
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = cm
            window = MainWindow()
        type(self)._windows.append(window)
        return window.snmp_panel

    def test_json_reads_1e309_as_infinity(self):
        """前提の確認: JSON の 1e309 は float の無限大になること。"""
        self.assertFalse(math.isfinite(json.loads("1e309")))

    def test_a_non_finite_limit_does_not_stop_the_window_from_opening(self):
        """非有限の上限でウィンドウが作れること（既定値へ落ちる）。"""
        cases = {"1e309": None, "Infinity": None, "-Infinity": None,
                 "NaN": None}
        for literal in cases:
            with self.subTest(max_traps=literal):
                panel = self._panel_from_json(literal)
                self.assertEqual(panel.max_traps, panel.DEFAULT_MAX_TRAPS,
                                 "既定値へ落ちていない: %r" % panel.max_traps)

    def test_the_limit_is_always_a_finite_positive_int(self):
        """上限として使う値が、有限の正の整数であること。"""
        panel = self._panel_from_json("1e309")
        self.assertIsInstance(panel.max_traps, int)
        self.assertTrue(math.isfinite(panel.max_traps))
        self.assertGreater(panel.max_traps, 0)

    def test_a_normal_limit_is_still_honoured(self):
        """まともな値はこれまでどおり効くこと（対照）。"""
        panel = self._panel_from_json("25")
        self.assertEqual(panel.max_traps, 25)


if __name__ == "__main__":
    unittest.main()
