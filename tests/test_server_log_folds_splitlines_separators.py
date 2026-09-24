"""サーバーパネルのログ: U+001C / U+001D / U+001E が畳まれず、エクスポートした
ログが str.splitlines() で割れて偽の行に見える件。

何が起きていたか（実測、基準 028ebc2。127.0.0.1 のみ）: TFTPServerPanel を本物の
サーバーで動かし、認証の要らない RRQ を 4 件送った。存在しないファイル名は
'missing' + 区切り文字 + '[12:00:00] [192.0.2.9] 転送完了: backup.cfg' で、
区切り文字は chr(0x1C) / chr(0x1D) / chr(0x1E) / U+2028 を 1 件ずつ。画面は
4 ブロックで、toPlainText() を chr(10) で割っても 4 行だったが、splitlines()
では 7 行になった。エクスポートしたファイルも splitlines() で 7 行になり、偽の
「転送完了」が 3 つ独立した行に見えた。ui/plain_log.py の置き換え表
（_LINE_BREAKS）にこの 3 文字が無かったため。

経緯: tests/test_server_log_folds_frame_markers.py の docstring には「chr(0x1C)
〜chr(0x1E) は Qt の画面でもエクスポートでも行を割らない。str.splitlines()
だけが割るので、ここでは扱わない」とあり、当時は Qt が行を割るかどうかを
基準にして意図的に外していた。その後、Syslog のテキスト保存（f154016、
tests/test_syslog_export_folds_unicode_line_breaks.py）は、保存したログを読む
側（Python の splitlines()、Unicode の改行に対応したビューア）が割るかどうかを
基準にして、この 3 文字も畳むことにした。同じアプリの中で、Syslog は割れない
のにサーバーパネルのログは割れる、という食い違いが残っていた。どちらも
「保存したログに偽の記録行を混ぜられない」ことが目的なので、広い方
（splitlines() の基準）へそろえる。

どう直したか: plain_log の置き換え表に chr(0x1C) / chr(0x1D) / chr(0x1E) を
足し、Syslog と同じ見える表記（バックスラッシュ + 'x1c' / 'x1d' / 'x1e'）へ
畳む。値は捨てないので、要求名は画面にもエクスポートにも残る。既存の区切り
（改行・U+0085・U+2028/2029・U+FDD0/FDD1）の扱いは変えない。
"""
import os
import socket
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

BACKSLASH = chr(92)
FAKE_RECORD = "[12:00:00] [192.0.2.9] 転送完了: backup.cfg"

# splitlines() だけが割る 3 文字と、畳んだ後に残っているべき表記（Syslog と同じ）
SEPARATORS = [(chr(0x1C), BACKSLASH + "x1c"),
              (chr(0x1D), BACKSLASH + "x1d"),
              (chr(0x1E), BACKSLASH + "x1e")]


class ServerLogFoldsSplitlinesSeparatorsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _config(self):
        from core.config_manager import ConfigManager
        directory = tempfile.mkdtemp(prefix="netbelt-foldsplit-")
        return ConfigManager(config_path=os.path.join(directory,
                                                      "config.json"))

    def _send_rrqs(self, names):
        """127.0.0.1 の TFTPServer へ存在しないファイルの RRQ を送り、通知を返す"""
        import core.tftp_server as tftp_server
        root = tempfile.mkdtemp(prefix="netbelt-foldsplit-root-")
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        events = []
        server = tftp_server.TFTPServer(
            port=port, root_dir=root,
            on_event=lambda kind, ip, payload: events.append((kind, ip, payload)))
        server.start()
        self.addCleanup(server.stop)
        for name in names:
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.settimeout(3)
            self.addCleanup(client.close)
            client.sendto(struct.pack("!H", 1) + name.encode("utf-8")
                          + b"\0" + b"octet\0", ("127.0.0.1", server.port))
            try:
                client.recvfrom(4096)
            except OSError:
                pass
        deadline = time.time() + 5
        while time.time() < deadline and len(events) < len(names):
            time.sleep(0.05)
        self.assertEqual(len(events), len(names),
                         "TFTP サーバが通知を出しませんでした: %r" % (events,))
        return events

    def test_requests_from_the_network_keep_one_line_for_splitlines(self):
        """RRQ（127.0.0.1）の要求名に入れた 3 文字で、ログが splitlines() でも割れないこと。"""
        from ui.tftp_server_panel import TFTPServerPanel
        panel = TFTPServerPanel(config_manager=self._config())
        self.addCleanup(panel.close)
        names = [sep.join(["missing", FAKE_RECORD]) for sep, _ in SEPARATORS]
        for kind, ip, payload in self._send_rrqs(names):
            self.assertEqual(kind, "protocol_error")
            panel._on_protocol_event(ip, payload[0], payload[1], payload[2])

        text = panel.log_text.toPlainText()
        self.assertEqual(len(text.splitlines()), len(names),
                         "%d 件の通知が splitlines() で %d 行に割れています: %r"
                         % (len(names), len(text.splitlines()), text))
        for _sep, shown in SEPARATORS:
            self.assertIn(shown.join(["missing", FAKE_RECORD]), text,
                          "要求されたファイル名が残っていません: %r" % (text,))

    def test_every_panel_folds_the_separators(self):
        """3 パネルとも 3 文字を 1 行へ畳み、前後と表記が残ること。"""
        from ui.ftp_server_panel import FTPServerPanel
        from ui.sftp_server_panel import SFTPServerPanel
        from ui.tftp_server_panel import TFTPServerPanel

        panels = [("FTP", FTPServerPanel(config_manager=self._config())),
                  ("TFTP", TFTPServerPanel(config_manager=self._config())),
                  ("SFTP", SFTPServerPanel(config_manager=self._config()))]
        for _, panel in panels:
            self.addCleanup(panel.close)
        for name, panel in panels:
            for sep, shown in SEPARATORS:
                with self.subTest(panel=name, code=hex(ord(sep))):
                    panel.log_text.clear()
                    panel._add_log("さき%sあと" % sep)
                    text = panel.log_text.toPlainText()
                    self.assertEqual(len(text.splitlines()), 1,
                                     "%s: 1 件の通知が複数行に割れています: %r"
                                     % (name, text))
                    self.assertIn("さき%sあと" % shown, text,
                                  "%s: 区切りの前後か表記が落ちています: %r"
                                  % (name, text))

    def test_the_exported_file_has_one_line_per_entry(self):
        """エクスポートしたファイルも、splitlines() で 1 件 1 行になること。"""
        from ui.tftp_server_panel import TFTPServerPanel
        panel = TFTPServerPanel(config_manager=self._config())
        self.addCleanup(panel.close)
        for sep, _shown in SEPARATORS:
            panel._on_protocol_event(
                "192.0.2.5", sep.join(["missing", FAKE_RECORD]),
                "要求されたファイルがありません", "download")
        out = os.path.join(tempfile.mkdtemp(prefix="netbelt-foldsplit-out-"),
                           "tftp_log.txt")
        with mock.patch("ui.log_export.QFileDialog.getSaveFileName",
                        return_value=(out, "")), \
                mock.patch("ui.log_export.QMessageBox.information"), \
                mock.patch("ui.log_export.QMessageBox.warning"), \
                mock.patch("ui.log_export.QMessageBox.critical"):
            panel._on_export_log()
        with open(out, encoding="utf-8", newline="") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(len(lines), len(SEPARATORS),
                         "保存したログに偽の行が混ざっています: %r" % (lines,))
        for line in lines:
            self.assertFalse(line.startswith("[12:00:00]"),
                             "偽の記録が独立した行になっています: %r" % (lines,))

    def test_the_fold_covers_every_splitlines_separator(self):
        """str.splitlines() が区切りとみなす文字は、どれも 1 行へ畳まれること。"""
        from ui.plain_log import fold_to_one_line
        separators = [chr(cp) for cp in range(0x110000)
                      if not 0xD800 <= cp <= 0xDFFF
                      and len(("a" + chr(cp) + "b").splitlines()) > 1]
        self.assertTrue(separators)
        for sep in separators:
            with self.subTest(code=hex(ord(sep))):
                folded = fold_to_one_line("a" + sep + "b")
                self.assertEqual(len(folded.splitlines()), 1,
                                 "%s が畳まれていません: %r"
                                 % (hex(ord(sep)), folded))

    def test_the_notation_matches_the_syslog_export(self):
        """Syslog のテキスト保存と共通の文字は、同じ表記へ畳むこと（基準をそろえる）。"""
        from ui.plain_log import fold_to_one_line
        from ui.syslog_panel import _TEXT_LINE_BREAKS
        for char, shown in _TEXT_LINE_BREAKS:
            with self.subTest(code=hex(ord(char))):
                self.assertEqual(fold_to_one_line("a" + char + "b"),
                                 "a" + shown + "b")


if __name__ == "__main__":
    unittest.main()
