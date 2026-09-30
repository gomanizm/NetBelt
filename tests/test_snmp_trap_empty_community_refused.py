"""何も受け付けない設定で Trap 受信を始めて「受信中」と出さないことを検証する。

何が起きていたか（実測、基準 441ea02）: Trap 受信タブで、バージョン「v1/v2c」＋
Community 空（"" と "   "）、「両方」＋ Community 空＋ v3 ユーザ名空の 3 通りで
受信開始を押すと、どれも表示は「🔵 受信中 (ポート N)」、受信スレッドは実行中、
登録は communities=[] / v3_users=[] だった。警告は 0 回、error_occurred も
空。この状態で localhost へ v2c Trap を送ると、コミュニティ "" も "public" も
受理されなかった。core の _clean_communities が空文字（空白だけも strip で
空になる）を落とし、登録する資格情報が 1 つも無くても bind は成功を返す。
v3 側には「黙って開始すると、起動したのに何も来ない状態になる」ことを理由に
した事前の検査があるが、v1/v2c 側には同じ検査が無かった。

利用者の決定（snmp-x1 (A)）: 何も受けない設定（Community が空で v3 も無い等）
だけを断る。「両方」で Community が空でも、v3 ユーザが設定済みなら開始する。

どう直したか: _trap_community_input_error を足し、受信開始の v3 の検査の次に
呼ぶ。バージョンが「v3」以外で Community が（前後の空白を落として）空、かつ
v3 ユーザも無い（「v1/v2c」か、「両方」で v3 ユーザ名が空）ときに警告して
止める。空欄を「全部受ける」とは解釈しない（空文字の登録は意図せず
「コミュニティ無しを受け入れる」設定になるので core で塞いである）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

ENGINE_ID = "8000000001020304"


class SnmpTrapEmptyCommunityRefusedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _start(self, version, community, v3_user=""):
        """設定を入れて受信開始を押す。パネルと警告の mock を返す。"""
        from ui.snmp_panel import SNMPPanel
        # MIB の読み込みはこの検査と関係が無いので走らせない
        with mock.patch.object(SNMPPanel, "_start_background_mib_loading"):
            panel = SNMPPanel()
        self.addCleanup(panel.deleteLater)
        panel.mib_loading = False
        panel.mib_loaded = True
        # 実際にポートは開けない。開始を頼まれたかどうかだけ見る
        panel.snmp_manager = mock.Mock()
        panel.snmp_manager.start_trap_receiver.return_value = True
        panel.trap_version_combo.setCurrentText(version)
        panel.trap_community_edit.setText(community)
        panel.trap_v3_username_edit.setText(v3_user)
        if v3_user:
            panel.trap_v3_engine_ids_edit.setPlainText(ENGINE_ID)
        # 検証に失敗するとモーダルを出す。offscreen では誰も閉じられない
        with mock.patch("ui.snmp_panel.QMessageBox.warning") as warn:
            panel._on_trap_start_clicked()
        return panel, warn

    def _assert_refused(self, panel, warn):
        warn.assert_called_once()
        self.assertIn("Community", warn.call_args[0][2])
        panel.snmp_manager.start_trap_receiver.assert_not_called()
        self.assertIn("停止中", panel.trap_status_label.text())
        self.assertFalse(panel.trap_start_button.isHidden())

    def _assert_started(self, panel, warn):
        warn.assert_not_called()
        panel.snmp_manager.start_trap_receiver.assert_called_once()
        self.assertIn("受信中", panel.trap_status_label.text())

    def test_v1v2c_with_an_empty_community_is_refused(self):
        ideographic_space = chr(0x3000)   # 全角の空白（core は strip で落とす）
        for text in ("", "   ", "\t", ideographic_space):
            with self.subTest(community=text):
                self._assert_refused(*self._start("v1/v2c", text))

    def test_both_with_no_community_and_no_v3_user_is_refused(self):
        for text in ("", "   "):
            with self.subTest(community=text):
                self._assert_refused(*self._start("両方", text))

    def test_both_with_a_v3_user_and_no_community_still_starts(self):
        """決定 (A): v3 は受けられるので止めない。"""
        panel, warn = self._start("両方", "", v3_user="netbelt-v3")
        self._assert_started(panel, warn)
        users = panel.snmp_manager.start_trap_receiver.call_args[0][2]
        self.assertEqual(len(users), 1)

    def test_a_community_still_starts(self):
        """対照: Community があれば従来どおり始まること。"""
        for version in ("両方", "v1/v2c"):
            with self.subTest(version=version):
                self._assert_started(*self._start(version, "public"))

    def test_v3_only_does_not_need_a_community(self):
        """対照: 「v3」では Community を使わないので、空でも始まること。"""
        self._assert_started(*self._start("v3", "", v3_user="netbelt-v3"))


if __name__ == "__main__":
    unittest.main()
