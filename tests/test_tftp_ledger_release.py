"""TFTPServerManager の重複コアレス台帳（_tx）を、失敗の種類によらず降ろすこと。

実測: 台帳から降ろす処理が「理由文字列に『タイムアウト』を含むとき」だけに
結び付いていた。確立後に出る「アップロード失敗（保存できません）: ...」や
「ダウンロード失敗: ...」では count が減らず、項目が count=1 のまま残る。
残ると (ip, filename, direction) が二度と「初回」にならず、以後その組み合わせの
transfer_started が UI へ出なくなる。さらに完了で done=True が焼き付くと、
本物のタイムアウトまで握り潰される。

握り潰してよいのは「重複要求の敗者が出す偽タイムアウト」だけなので、
台帳を降ろす処理と、握り潰すかどうかの判定は分けて考える。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TftpLedgerReleaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _manager(self):
        from core.tftp_server import TFTPServerManager
        m = TFTPServerManager()
        started, errors = [], []
        m.transfer_started.connect(lambda ip, fn, t, d: started.append((ip, fn, d)))
        m.protocol_event.connect(
            lambda ip, fn, reason, d: errors.append((ip, fn, reason, d)))
        return m, started, errors

    def test_non_timeout_failure_releases_the_ledger(self):
        """確立後の非タイムアウト失敗でも台帳から降ろし、次の開始を出せること。"""
        m, started, errors = self._manager()
        ip, fn = "192.0.2.31", "running-config"

        m._on_event("transfer_started", ip, (fn, 100, "upload"))
        m._on_event("protocol_error", ip,
                    (fn, "アップロード失敗（保存できません）: disk full", "upload"))
        self.app.processEvents()

        self.assertEqual(len(errors), 1, "本物の失敗が UI へ出ていない")
        self.assertEqual(dict(m._tx), {},
                         "失敗した転送が台帳に残り続けている")

        # 同じ機器が同じファイルをもう一度上げてきたら、また開始を出すこと
        m._on_event("transfer_started", ip, (fn, 100, "upload"))
        self.app.processEvents()
        self.assertEqual(len(started), 2, "2 回目の転送開始が UI へ出ていない")

    def test_a_genuine_timeout_survives_an_earlier_failure(self):
        """失敗の残留で done=True が焼き付き、本物のタイムアウトを消さないこと。"""
        m, started, errors = self._manager()
        ip, fn = "192.0.2.31", "ios.bin"

        m._on_event("transfer_started", ip, (fn, 100, "download"))
        m._on_event("protocol_error", ip,
                    (fn, "ダウンロード失敗: [Errno 5] I/O error", "download"))
        m._on_event("transfer_started", ip, (fn, 100, "download"))
        m._on_event("transfer_complete", ip, (fn, 100, 100, "download"))
        m._on_event("transfer_started", ip, (fn, 100, "download"))
        m._on_event("protocol_error", ip,
                    (fn, "ダウンロードがタイムアウト", "download"))
        self.app.processEvents()

        reasons = [e[2] for e in errors]
        self.assertIn("ダウンロードがタイムアウト", reasons,
                      "全滅したはずの転送のタイムアウトが握り潰されている")
        self.assertEqual(dict(m._tx), {}, "台帳に項目が残っている")

    def test_duplicate_request_timeouts_are_still_suppressed(self):
        """重複要求の敗者が出す偽タイムアウトは、これまで通り握り潰すこと。"""
        m, started, errors = self._manager()
        ip, fn = "192.0.2.31", "itch-setup.exe"

        for _ in range(4):
            m._on_event("transfer_started", ip, (fn, 100, "download"))
        m._on_event("transfer_complete", ip, (fn, 100, 100, "download"))
        for _ in range(3):
            m._on_event("protocol_error", ip,
                        (fn, "ダウンロードがタイムアウト", "download"))
        self.app.processEvents()

        self.assertEqual(len(started), 1)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
