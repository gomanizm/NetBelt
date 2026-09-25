"""srv-03 の積み残し: Qt が段落の区切りに使う U+FDD0 / U+FDD1 で、ログの行がまだ割れる件。

何が起きていたか（実測、基準 097550c）: 直前の修正で、3 パネルのログは
ui/plain_log.append_line() が insertText の前に改行を見える表記へ畳むように
なった。ところが QTextCursor.insertText は chr(10)・chr(13)・U+2029 のほかに、
QTextBeginningOfFrame（U+FDD0）と QTextEndOfFrame（U+FDD1）でも新しい
ブロックを作り、toPlainText() はこの 2 文字を chr(10) にして返す。
置き換え表（_LINE_BREAKS）にはこの 2 文字が無かった。

  127.0.0.1 の TFTPServer へ RRQ 1 件（無認証・存在しないファイル名でよい）。
  ファイル名を
  'missing' + chr(0xFDD0) + '[12:00:00] [192.0.2.9] 転送完了: backup.cfg'
  にして UTF-8 で送ると（TFTP 側は utf-8/replace で復号するのでそのまま通る）、
  TFTPServerPanel._on_protocol_event の後、blockCount は 2 で、
  log_text.toPlainText() は

    0: '[02:24:37] [127.0.0.1] 要求されたファイルがありません: missing'
    1: '[12:00:00] [192.0.2.9] 転送完了: backup.cfg'

  の 2 行になり、2 行目が本物の完了記録に見えた。chr(0xFDD1) でも同じ。
  FTP・SFTP パネルの _add_log でも blockCount が 2 になった。

エクスポートは toPlainText() をそのまま書くので、偽の完了行はファイルにも
残る。区切りは chr(10) に置き換わっているので、元の要求名も復元できない。
BMP の全文字を append_line へ 1 文字ずつ通した調べでは、ブロックが増えるのは
この 2 文字だけだった（chr(0x1C)〜chr(0x1E) は Qt の画面でもエクスポートでも
行を割らない。str.splitlines() だけが割るので、ここでは扱わない）。

どう直したか: plain_log の置き換え表に U+FDD0 / U+FDD1 を足し、ほかの改行と
同じく見える表記（バックスラッシュ + 'ufdd0' / 'ufdd1'）へ畳む。値は捨てない
ので、要求名は画面にもエクスポートにも残る。
"""
import os
import socket
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, "src")

BACKSLASH = chr(92)
FAKE_RECORD = "[12:00:00] [192.0.2.9] 転送完了: backup.cfg"

# Qt が段落の区切りとして扱う文字と、畳んだ後に残っているべき表記
FRAME_MARKERS = [(chr(0xFDD0), BACKSLASH + "ufdd0"),
                 (chr(0xFDD1), BACKSLASH + "ufdd1")]


class ServerLogFoldsFrameMarkersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _config(self):
        from core.config_manager import ConfigManager
        directory = tempfile.mkdtemp(prefix="netbelt-foldframe-")
        return ConfigManager(config_path=os.path.join(directory,
                                                      "config.json"))

    def _assert_one_line(self, text_edit, label):
        blocks = text_edit.document().blockCount()
        text = text_edit.toPlainText()
        self.assertEqual(blocks, 1,
                         "%s: 1 件の通知が %d ブロックに割れています: %r"
                         % (label, blocks, text))
        self.assertEqual(len(text.splitlines()), 1,
                         "%s: 1 件の通知が複数行に割れています: %r"
                         % (label, text))
        return text

    def test_a_frame_marker_in_a_request_from_the_network_keeps_one_line(self):
        """RRQ 1 件（127.0.0.1）でログが割れず、要求名も残ること。"""
        import core.tftp_server as tftp_server
        from ui.tftp_server_panel import TFTPServerPanel

        for marker, shown in FRAME_MARKERS:
            with self.subTest(code=hex(ord(marker))):
                panel = TFTPServerPanel(config_manager=self._config())
                self.addCleanup(panel.close)

                root = tempfile.mkdtemp(prefix="netbelt-foldframe-root-")
                probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
                probe.close()

                events = []
                server = tftp_server.TFTPServer(
                    port=port, root_dir=root,
                    on_event=lambda kind, ip, payload, sink=events:
                        sink.append((kind, ip, payload)))
                server.start()
                self.addCleanup(server.stop)

                client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                client.settimeout(3)
                self.addCleanup(client.close)
                name = marker.join(["missing", FAKE_RECORD])
                client.sendto(struct.pack("!H", 1) + name.encode("utf-8")
                              + b"\0" + b"octet\0",
                              ("127.0.0.1", server.port))
                try:
                    client.recvfrom(4096)
                except OSError:
                    pass

                deadline = time.time() + 5
                while time.time() < deadline and not events:
                    time.sleep(0.05)
                self.assertTrue(events, "TFTP サーバが通知を出しませんでした")
                kind, ip, payload = events[0]
                self.assertEqual(kind, "protocol_error")
                panel._on_protocol_event(ip, payload[0], payload[1],
                                         payload[2])

                text = self._assert_one_line(panel.log_text, "TFTP RRQ")
                self.assertIn(shown.join(["missing", FAKE_RECORD]), text,
                              "要求されたファイル名が残っていません: %r"
                              % (text,))

    def test_every_panel_folds_the_frame_markers(self):
        """3 パネルとも U+FDD0 / U+FDD1 を 1 行へ畳み、前後が残ること。"""
        from ui.ftp_server_panel import FTPServerPanel
        from ui.sftp_server_panel import SFTPServerPanel
        from ui.tftp_server_panel import TFTPServerPanel

        panels = [("FTP", FTPServerPanel(config_manager=self._config())),
                  ("TFTP", TFTPServerPanel(config_manager=self._config())),
                  ("SFTP", SFTPServerPanel(config_manager=self._config()))]
        for _, panel in panels:
            self.addCleanup(panel.close)
        for name, panel in panels:
            for marker, shown in FRAME_MARKERS:
                with self.subTest(panel=name, code=hex(ord(marker))):
                    panel.log_text.clear()
                    panel._add_log("さき%sあと" % marker)
                    text = self._assert_one_line(panel.log_text, name)
                    self.assertIn("さき%sあと" % shown, text,
                                  "%s: 区切りの前後か表記が落ちています: %r"
                                  % (name, text))

    def test_the_folded_line_is_what_gets_exported(self):
        """エクスポートされる文字列（toPlainText）にも偽の行が残らないこと。"""
        from ui.tftp_server_panel import TFTPServerPanel

        for marker, shown in FRAME_MARKERS:
            with self.subTest(code=hex(ord(marker))):
                panel = TFTPServerPanel(config_manager=self._config())
                self.addCleanup(panel.close)
                panel._on_protocol_event(
                    "192.0.2.5", marker.join(["missing", FAKE_RECORD]),
                    "要求されたファイルがありません", "download")

                exported = panel.log_text.toPlainText()
                self.assertNotIn(chr(10), exported,
                                 "偽の行がエクスポートへ残ります: %r"
                                 % (exported,))
                self.assertIn(shown.join(["missing", FAKE_RECORD]), exported,
                              "要求されたファイル名が失われています: %r"
                              % (exported,))


if __name__ == "__main__":
    unittest.main()
