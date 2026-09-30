"""GET/WALK も Trap 受信と同じく、Community の前後の空白を落として送ることを検証する。

何が起きていたか（実測、基準 441ea02）: Trap 受信タブの Community に " ops " と
入れて受信を始めると、登録されるのは "ops"（core の _clean_communities が
strip する）で、" ops " を名乗る Trap は受けず "ops" の Trap を受けた。一方
GET/WALK タブで同じ " ops " を入れると、_collect_request_params が加工せずに
渡し、localhost の偽エージェントに届いたコミュニティは " ops "（空白付き）
だった。同じ入力が、タブによって別の値として使われていた。資料から貼った
ときに末尾の空白が付くと、GET/WALK だけが黙って時間切れになる。

利用者の決定（snmp-01 (a)）: GET/WALK でも前後の空白を落とし、Trap 側と揃える。

どう直したか: _collect_request_params で community_edit の値を strip してから
渡す（ホスト欄・v3 ユーザ名と同じ扱い）。中の空白はそのまま残す。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

HOST = "192.0.2.10"
OID = "1.3.6.1.2.1.1.1.0"


class SnmpGetWalkCommunityStripTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.snmp_panel import SNMPPanel
        # MIB の読み込みはこの検査と関係が無いので走らせない
        with mock.patch.object(SNMPPanel, "_start_background_mib_loading"):
            self.panel = SNMPPanel()
        self.addCleanup(self.panel.deleteLater)
        # 実際の通信はしない。渡された値だけを見る
        self.panel.snmp_manager = mock.Mock()
        self.panel.host_edit.setText(HOST)
        self.panel.oid_edit.setText(OID)
        # 検証に失敗したときにモーダルで止まらないよう塞ぐ
        patcher = mock.patch("ui.snmp_panel.QMessageBox")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _sent_community(self, op, version, text):
        self.panel.version_combo.setCurrentText(version)
        self.panel.community_edit.setText(text)
        getattr(self.panel, "_on_%s_clicked" % op)()
        call = getattr(self.panel.snmp_manager, "snmp_" + op).call_args
        self.assertIsNotNone(call, "%s の要求が出ていない" % op)
        return call.kwargs["community"]

    def test_get_and_walk_drop_the_surrounding_spaces(self):
        for op in ("get", "walk"):
            for version in ("v1", "v2c"):
                with self.subTest(op=op, version=version):
                    self.assertEqual(
                        self._sent_community(op, version, " netbelt-ro "),
                        "netbelt-ro",
                        "前後の空白を付けたままコミュニティとして送っている")

    def test_the_same_text_means_the_same_community_on_both_tabs(self):
        """GET/WALK が送る値と、Trap 受信が登録する値が同じであること。"""
        from core.snmp_manager import _clean_communities
        ideographic_space = chr(0x3000)   # 全角の空白
        for text in (" ops ", "\tops\t", "ops" + ideographic_space,
                     "  ops"):
            with self.subTest(text=text):
                self.assertEqual(self._sent_community("get", "v2c", text),
                                 _clean_communities([text])[0])

    def test_spaces_inside_the_community_are_kept(self):
        """落とすのは前後だけ。中の空白は機器側の綴りの一部として残す。"""
        self.assertEqual(self._sent_community("walk", "v2c", " my ro com "),
                         "my ro com")

    def test_a_community_without_spaces_is_sent_as_is(self):
        """対照: 空白の無い値は従来どおりそのまま送る。"""
        self.assertEqual(self._sent_community("get", "v2c", "netbelt-ro"),
                         "netbelt-ro")


if __name__ == "__main__":
    unittest.main()
