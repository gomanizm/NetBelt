"""srv-03 の回帰テスト: ログへ届いた文字列の改行で、行がまだ割れる件。

何が起きていたか（実測、基準 aa38a2b）: 直前の修正で 3 パネルのログは
QTextEdit.append() をやめ ui/plain_log.append_line() へ寄せたが、閉じたのは
リッチテキスト判定（<br> の解釈）だけで、届いた文字列そのものは素通しだった。
<br> の代わりに生の改行を入れると、同じ被害がそのまま出る。

  127.0.0.1 の TFTPServer へ RRQ 1 件（TFTP は無認証で、存在しない
  ファイル名でよい）。ファイル名を
  'missing' + chr(10) + '[12:00:00] [192.0.2.9] 転送完了: backup.cfg'
  にすると、TFTPServerPanel._on_protocol_event の後の
  log_text.toPlainText() は

    0: '[11:28:09] [127.0.0.1] 要求されたファイルがありません: missing'
    1: '[12:00:00] [192.0.2.9] 転送完了: backup.cfg'

  の 2 行になり、2 行目が本物の完了記録に見えた。

エクスポートは toPlainText() をそのまま書き出すので、偽の完了行はファイルにも
残り、元の要求名は復元できない。行を割れる文字は chr(10) / chr(13) / CRLF /
chr(11) / chr(12) / U+2028 / U+2029 を実測で確認した。FTP・SFTP も同じ
append_line を使うので同様（ただし名前が届くのはログイン後）。

どう直したか: append_line() が insertText の前に行を 1 行へ畳む。C0 の
CR/LF/VT/FF と U+0085・U+2028・U+2029 を見える表記（chr(10) なら
バックスラッシュ + n）へ置き換えるので、要求名は失われないまま 1 行に収まる。
core 側は今までどおり生の文字列を渡す（表示層で閉じる方針は変えない）。
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
NEWLINE = chr(0x0A)

# 偽の「転送完了」行に見せかける要求ファイル名（生の改行を使う）
INJECTION = NEWLINE.join(
    ["missing", "[12:00:00] [192.0.2.9] 転送完了: backup.cfg"])
# 畳んだ後に残っているべき表記
FOLDED = (BACKSLASH + "n").join(
    ["missing", "[12:00:00] [192.0.2.9] 転送完了: backup.cfg"])

# append_line 経由で行を割れることを実測した文字
LINE_BREAKS = [chr(0x0A), chr(0x0D), chr(0x0D) + chr(0x0A), chr(0x0B),
               chr(0x0C), chr(0x85), chr(0x2028), chr(0x2029)]


class ServerLogFoldsLineBreaksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _config(self):
        from core.config_manager import ConfigManager
        directory = tempfile.mkdtemp(prefix="netbelt-foldlog-")
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

    def test_a_newline_in_a_request_from_the_network_keeps_one_line(self):
        """RRQ 1 件（127.0.0.1）でログが割れず、要求名も残ること。"""
        import core.tftp_server as tftp_server
        from ui.tftp_server_panel import TFTPServerPanel

        panel = TFTPServerPanel(config_manager=self._config())
        self.addCleanup(panel.close)

        root = tempfile.mkdtemp(prefix="netbelt-foldlog-root-")
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

        deadline = time.time() + 5
        while time.time() < deadline and not events:
            time.sleep(0.05)
        self.assertTrue(events, "TFTP サーバが通知を出しませんでした")
        kind, ip, payload = events[0]
        self.assertEqual(kind, "protocol_error")
        panel._on_protocol_event(ip, payload[0], payload[1], payload[2])

        lines = panel.log_text.toPlainText().splitlines()
        self.assertEqual(len(lines), 1,
                         "RRQ 1 件でログが割れています: %r" % (lines,))
        self.assertIn(FOLDED, lines[0],
                      "要求されたファイル名が残っていません: %r" % (lines,))

    def test_every_line_breaking_character_is_folded(self):
        """行を割れる文字はどれも 1 行へ畳まれ、前後が残ること。"""
        for name, panel in self._panels():
            for char in LINE_BREAKS:
                with self.subTest(panel=name, code=[ord(c) for c in char]):
                    panel.log_text.clear()
                    panel._add_log("さき%sあと" % char)
                    text = panel.log_text.toPlainText()
                    lines = text.splitlines()
                    self.assertEqual(
                        len(lines), 1,
                        "%s: 1 件の通知が %d 行に割れています: %r"
                        % (name, len(lines), text))
                    self.assertIn("さき", lines[0])
                    self.assertIn("あと", lines[0],
                                  "%s: 改行の後ろが落ちています: %r"
                                  % (name, text))

    def test_the_folded_line_is_what_gets_exported(self):
        """エクスポートされる文字列（toPlainText）にも偽の行が残らないこと。"""
        from ui.tftp_server_panel import TFTPServerPanel

        panel = TFTPServerPanel(config_manager=self._config())
        self.addCleanup(panel.close)

        panel._on_protocol_event("192.0.2.5", INJECTION,
                                 "要求されたファイルがありません", "download")

        exported = panel.log_text.toPlainText()
        self.assertEqual(len(exported.splitlines()), 1,
                         "偽の行がエクスポートへ残ります: %r" % (exported,))
        self.assertIn(FOLDED, exported,
                      "要求されたファイル名が失われています: %r" % (exported,))


if __name__ == "__main__":
    unittest.main()
