"""サーバーパネルのアクティビティログが平文のまま積まれることの回帰テスト。

何が起きていたか（実測、基準 f4cad23）: FTP / TFTP / SFTP の 3 パネルの
_add_log は QTextEdit.append() を使っていた。append() は渡された文字列が
リッチテキストらしければ HTML として解釈するので、要求ファイル名に <br> を
入れるだけでログ 1 行が 2 行に割れた。TFTP は認証が無く、存在しない
ファイルへの RRQ でも通知が出るため、届く相手なら誰でも偽の行を差し込める。

    ファイル名 'missing<br>[12:00:00] [192.0.2.9] 転送完了: backup.cfg' で RRQ
    -> 0: '[10:33:41] [192.0.2.5] 要求されたファイルがありません: missing'
       1: '[12:00:00] [192.0.2.9] 転送完了: backup.cfg'
       エクスポートされる文字列に <br> が残るか: False

エクスポート（log_export）は toPlainText() をそのまま保存するので、偽の行は
ファイルにも残り、元の要求文字列は失われる（監査の材料にならない）。

どう直したか: ui/plain_log.py の append_line() へ寄せ、QTextCursor.insertText
で 1 行足す。リッチテキストの判定を通らないので、届いた文字列はそのまま
1 行として残る。行数上限（document().setMaximumBlockCount）と末尾への自動
スクロールはこれまでどおり。core 側は生の文字列を渡したままにしてある。
"""
import os
import socket
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, "src")

# 偽の「転送完了」行に見せかける要求ファイル名（Windows で作成可能かは無関係）
INJECTION = "missing<br>[12:00:00] [192.0.2.9] 転送完了: backup.cfg"


class ServerLogPlainTextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _config(self):
        from core.config_manager import ConfigManager
        directory = tempfile.mkdtemp(prefix="netbelt-plainlog-")
        return ConfigManager(config_path=os.path.join(directory,
                                                      "config.json"))

    def _panels(self):
        from ui.ftp_server_panel import FTPServerPanel
        from ui.sftp_server_panel import SFTPServerPanel
        from ui.tftp_server_panel import TFTPServerPanel
        panels = [("FTP", FTPServerPanel(config_manager=self._config())),
                  ("TFTP", TFTPServerPanel(config_manager=self._config())),
                  ("SFTP", SFTPServerPanel(config_manager=self._config()))]
        for _, panel in panels:
            self.addCleanup(panel.close)
        return panels

    def test_a_single_message_stays_a_single_line(self):
        for name, panel in self._panels():
            with self.subTest(panel=name):
                panel._add_log("[192.0.2.5] 要求されたファイルがありません: %s"
                               % INJECTION)
                text = panel.log_text.toPlainText()
                self.assertEqual(
                    len(text.splitlines()), 1,
                    "%s: 1 件の通知がログで %d 行に割れています: %r"
                    % (name, len(text.splitlines()), text))
                self.assertIn(
                    "<br>", text,
                    "%s: 届いた文字列が書き換えられています: %r" % (name, text))

    def test_tftp_protocol_event_keeps_the_requested_name(self):
        from ui.tftp_server_panel import TFTPServerPanel
        panel = TFTPServerPanel(config_manager=self._config())
        self.addCleanup(panel.close)

        panel._on_protocol_event("192.0.2.5", INJECTION,
                                 "要求されたファイルがありません", "download")

        lines = panel.log_text.toPlainText().splitlines()
        self.assertEqual(len(lines), 1,
                         "偽の行を差し込めています: %r" % (lines,))
        self.assertIn(INJECTION, lines[0],
                      "要求されたファイル名が失われています: %r" % (lines,))

    def test_tftp_request_from_the_network_keeps_one_line(self):
        """実際に RRQ を投げても行が割れないこと（127.0.0.1 のみ）"""
        import core.tftp_server as tftp_server
        from ui.tftp_server_panel import TFTPServerPanel

        panel = TFTPServerPanel(config_manager=self._config())
        self.addCleanup(panel.close)

        root = tempfile.mkdtemp(prefix="netbelt-plainlog-root-")
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        events = []
        server = tftp_server.TFTPServer(
            port=port, root_dir=root,
            on_event=lambda kind, ip, payload: events.append((kind, ip,
                                                              payload)))
        server.start()
        self.addCleanup(server.stop)

        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(3)
        self.addCleanup(client.close)
        client.sendto(struct.pack("!H", 1) + INJECTION.encode() + b"\0"
                      + b"octet\0", ("127.0.0.1", server.port))
        try:
            client.recvfrom(4096)
        except OSError:
            pass

        deadline = time.time() + 3
        while time.time() < deadline and not events:
            time.sleep(0.05)
        self.assertTrue(events, "TFTP サーバが通知を出しませんでした")
        kind, ip, payload = events[0]
        self.assertEqual(kind, "protocol_error")
        panel._on_protocol_event(ip, payload[0], payload[1], payload[2])

        lines = panel.log_text.toPlainText().splitlines()
        self.assertEqual(len(lines), 1,
                         "RRQ 1 件でログが割れています: %r" % (lines,))

    def test_line_limit_and_autoscroll_still_work(self):
        for name, panel in self._panels():
            with self.subTest(panel=name):
                limit = panel.MAX_LOG_LINES
                for i in range(limit + 20):
                    panel._add_log("行 %d" % i)
                lines = panel.log_text.toPlainText().splitlines()
                self.assertLessEqual(
                    len(lines), limit,
                    "%s: 行数の上限が効いていません (%d 行)" % (name, len(lines)))
                self.assertIn("行 %d" % (limit + 19), lines[-1],
                              "%s: 最新の行が末尾にありません" % name)
                scrollbar = panel.log_text.verticalScrollBar()
                self.assertEqual(scrollbar.value(), scrollbar.maximum(),
                                 "%s: 末尾まで送られていません" % name)


if __name__ == "__main__":
    unittest.main()
