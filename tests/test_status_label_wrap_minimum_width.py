"""状態表示ラベルの折り返しを外したら落ちる回帰テスト（GUI2-d）。

何が起きていたか（実測、基準 63c27f5）:
tests/test_panel_minimum_width.py の
test_long_status_text_does_not_widen_the_panels は、3 つの状態表示ラベル
（SNMP の実行状況・Trap の受信状態、Syslog の状況）から
setWordWrap(True) を 3 つとも外しても素通りした。理由は 2 つある。

1. 流し込む文面が短かった。使っていたのは 41 文字の例で、折り返しを
   外しても要求する最小幅は SNMP パネル 376px・Syslog パネル 460px に
   しかならず、上限の 520px に届かなかった。実際にそのラベルへ入る
   最長の文面はもっと長い（GET/WALK の実行状況は WALK の途中終了で
   86 文字、Trap の受信状態はファイアウォール許可の失敗で 129 文字、
   Syslog の状況も同じく失敗で 179 文字）。折り返しを外して実測すると
   SNMP パネル 638px / 1322px・Syslog パネル 1469px。

2. 測り方が古い値を返していた。Trap の受信状態ラベルは QGroupBox の
   中にあり、setText() による無効化はその QGroupBox 止まりで、親の
   タブページまでは Qt のイベントループ（LayoutRequest）経由でしか
   届かない。テストはイベントループを回さないので、折り返しを外して
   129 文字を入れても SNMP パネルは組み立て時の 376px を返し続けた。
   窓を表示してイベントループを回して同じことをすると、SNMP パネルは
   1322px を要求し、パネルを載せている QScrollArea に 1215px ぶんの
   横スクロールが出る（実測）。つまり不具合は現実に出ていて、
   テストだけが見逃していた。

どう直したか: パネルごとに「そのラベルへ実際に入る最長の文面」を、
パネル自身の経路（_on_fw_allow / _on_operation_completed）で入れてから
測る。測る前に配下のウィジェットとレイアウトの寸法キャッシュを捨てて、
イベントループを回したときと同じ値を得る（上の 1322px は、キャッシュを
捨てて測った値とイベントループを回して測った値が一致する）。
パネル別に subTest へ分けてあるので、1 つが落ちても残りの assert まで届く。
折り返しがある今の実装での実測は SNMP パネル 376px・Syslog パネル 243px。

520px という上限は、縮めた画面でもボタン行が 1 行で収まる幅として
利用者が決めた確認幅（2026-09-23）。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

# 縮めたときに収まっていてほしい幅（利用者の決定）
MAX_MIN_WIDTH = 520

# frozen(exe) でない環境では出ないが、配布物ではこの名前で出る自exe許可ルール。
# core.firewall._self_rule_name() が作る名前と同じもの
SELF_RULE_NAME = "NetBelt - app inbound (self)"


def _longest_walk_interrupt_reason():
    """WALK が途中で切れた理由として入りうる最長の文字列。

    理由は pysnmp の errorIndication をそのまま文字列にしたもの
    （core/snmp_manager.py の _perform_walk）。取りこぼさないよう
    pysnmp 側の定義から最長のものを拾う。
    """
    from pysnmp.proto import errind
    texts = [str(v) for v in vars(errind).values()
             if isinstance(v, errind.ErrorIndication)]
    return max(texts, key=len)


def _all_failed_message(reasons):
    """ファイアウォール許可が全部失敗したときの msg を本物の連結規則で作る。"""
    from core.firewall import combine_results
    _ok, msg = combine_results([(False, r) for r in reasons])
    return msg


def _repair_not_reflected(service, proto, port):
    """ensure_inbound_allow が返す失敗文面のうち最長のもの。"""
    from core.firewall import rule_name
    return ("許可ルールの修復を要求したが反映を確認できず: "
            + rule_name(service, proto, port))


def _self_not_reflected():
    """ensure_self_program_allow が返す失敗文面。"""
    return "自exe受信許可を要求したが反映を確認できず: " + SELF_RULE_NAME


def _fill_snmp_walk_status(window):
    """GET/WALK の実行状況へ、途中終了の文面をパネル自身の経路で入れる。"""
    panel = window.snmp_panel
    panel._on_operation_partial(_longest_walk_interrupt_reason())
    # 件数の桁も文面の長さに効くので、WALK で現実にありうる大きさにする
    rows = [("1.3.6.1.2.1.2.2.1.2.1", "OctetString", "GigabitEthernet0/0")] * 65535
    panel._on_operation_completed(True, rows)
    return (("SNMP パネル", panel),
            ("SNMP の GET / WALK タブ", panel.main_tabs.widget(0)))


def _fill_snmp_trap_status(window):
    """Trap の受信状態へ、ファイアウォール許可の失敗をパネル自身の経路で入れる。"""
    panel = window.snmp_panel
    msg = _all_failed_message([
        _repair_not_reflected("SNMP Trap", "UDP", panel.trap_port_spinbox.value()),
        _self_not_reflected(),
    ])
    with mock.patch.object(panel.snmp_manager, "fix_firewall",
                           return_value=(False, msg)):
        panel._on_fw_allow()
    return (("SNMP パネル", panel),
            ("SNMP の Trap受信タブ", panel.main_tabs.widget(1)))


def _fill_syslog_status(window):
    """Syslog の状況へ、ファイアウォール許可の失敗をパネル自身の経路で入れる。"""
    panel = window.syslog_panel
    # UDP と TCP の両方を受信しているときが、理由が 3 つ並ぶ最長の場合
    msg = _all_failed_message([
        _repair_not_reflected("Syslog", "UDP", panel.udp_port_spin.value()),
        _repair_not_reflected("Syslog", "TCP", panel.tcp_port_spin.value()),
        _self_not_reflected(),
    ])
    with mock.patch.object(panel.syslog_receiver, "fix_firewall",
                           return_value=(False, msg)):
        panel._on_fw_allow()
    return (("Syslog パネル", panel),)


# (subTest 名, 文面を入れて測る先を返す関数)
LONGEST_STATUS_CASES = (
    ("SNMP GET/WALK の実行状況", _fill_snmp_walk_status),
    ("SNMP Trap の受信状態", _fill_snmp_trap_status),
    ("Syslog の状況", _fill_syslog_status),
)


class StatusLabelWrapMinimumWidthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        """一時フォルダの config.json を使う MainWindow を組む。"""
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-wrapwidth-")
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = cm
            window = MainWindow()
        self.addCleanup(window.close)
        return window

    def _settle_layout(self, root):
        """配下の寸法キャッシュを捨て、イベントループを回したのと同じ値にする。

        ラベルへ文字を入れても、間に QGroupBox のような別レイアウトが挟まると
        無効化はそこで止まり、親のページのレイアウトには LayoutRequest
        （イベントループ経由）でしか届かない。テストはイベントループを
        回さないので、捨てずに測ると組み立て時の幅が返る。
        """
        from PyQt6.QtWidgets import QLayout, QWidget
        for child in root.findChildren(QWidget):
            child.updateGeometry()
        root.updateGeometry()
        for layout in root.findChildren(QLayout):
            layout.invalidate()
        if root.layout() is not None:
            root.layout().invalidate()

    def _assert_fits(self, name, widget):
        width = widget.minimumSizeHint().width()
        self.assertLessEqual(
            width, MAX_MIN_WIDTH,
            "%s の最小幅が %d px で、%d px まで縮められない"
            % (name, width, MAX_MIN_WIDTH))

    def test_longest_status_text_does_not_widen_the_panels(self):
        """各ラベルへ入る最長の文面でも 520px まで縮められること。"""
        for case_name, fill in LONGEST_STATUS_CASES:
            with self.subTest(label=case_name):
                window = self._window()
                targets = fill(window)
                for _name, widget in targets:
                    self._settle_layout(widget)
                for name, widget in targets:
                    self._assert_fits(name, widget)


if __name__ == "__main__":
    unittest.main()
