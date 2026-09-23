"""旧 known_hosts の引き継ぎが、読めない行を黙って捨てないことを検証する。

何が起きていたか（基準 f4cad23 で実測）。
_import_legacy_known_hosts_unlocked が引き継ぐのは _known_hosts_entry_id
が値を返す行だけで、同関数は欄が 3 つ未満なら None を返す。旧
~/.terminal-tool/known_hosts に次の 4 行を置いて引き継ぐと:

  1: `127.0.0.1 ssh-ed25519`（3 欄未満）
  2: `# migration memo`
  3: 正常な ecdsa 行
  4: 鍵欄が壊れた `192.0.2.30 ...`

  引き継ぎ前の「読めない行」: 1 行目と 4 行目
  引き継ぎの警告: None / 引き継ぎ済みの目印: True（作られる）
  引き継ぎ後の known_hosts: 2・3・4 行目のみ（1 行目が消えている）
  127.0.0.1 への接続: 引き継ぎ前 → 中止 / 引き継ぎ後 → 中止しない
                      （lookup は None なので TOFU が何でも受け入れる）

`# memo` のような 2 語のコメント行も、同じ「3 欄未満」の規則で捨てられる
（3 語以上のコメントは ('#', 2 語目) として残る）。

失われるのは「このファイルは壊れている」という合図で、旧ファイルを
見ていれば断っていた接続先が、引き継ぎを境に黙って初回接続へ戻る。
目印ができた後は二度とやり直されない。

同じ関数が @cert-authority / @revoked の印を剥がさない点も直す。印付きの
行は (印, ホスト) で見分けられていたので、同じホストの @revoked 行が
鍵種別ごとに 2 行あると 1 行しか引き継がれなかった（実測）。

どう直したか。重複の見分けがつかない行（3 欄未満・コメント）は判定の
対象から外し、そのまま書き出すようにした。印は 1 つ読み飛ばしたうえで
見分けに含め、(印, ホスト, 鍵種別) の 3 つで比べる。
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

from core import config_manager                    # noqa: E402

SHORT_LINE = "127.0.0.1 ssh-ed25519"
COMMENT_LINE = "# memo"      # 2 語なので欄が 3 つに満たない


class LegacyImportBrokenLineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import paramiko
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()
        cls.key_rsa = paramiko.RSAKey.generate(2048)

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="netbelt-legacybroken-"))
        self.addCleanup(shutil.rmtree, str(self.home), True)
        (self.home / ".terminal-tool").mkdir()
        self.old_kh = self.home / ".terminal-tool" / "known_hosts"
        self.new_dir = self.home / ".netbelt"
        self.new_dir.mkdir()
        self.new_kh = self.new_dir / "known_hosts"
        # 引き継ぎ元は Path.home() から直に引くので、本物のホームを
        # 見にいかないよう差し替える
        home_patcher = mock.patch.object(Path, "home", return_value=self.home)
        home_patcher.start()
        self.addCleanup(home_patcher.stop)
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.new_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _line(self, hostname, key):
        return "%s %s %s" % (hostname, key.get_name(), key.get_base64())

    def _marked(self, marker, hostname, key):
        return "%s %s" % (marker, self._line(hostname, key))

    def _write(self, path, lines):
        path.write_text("".join(line + "\n" for line in lines),
                        encoding="utf-8")

    def _import(self, old_lines, new_lines=None):
        """旧・新を用意して引き継ぎ、引き継ぎ後の行を返す。"""
        self._write(self.old_kh, old_lines)
        if new_lines is not None:
            self._write(self.new_kh, new_lines)
        warning = config_manager._import_legacy_known_hosts_unlocked(
            self.new_dir)
        self.assertIsNone(warning, "引き継ぎが失敗している: %s" % warning)
        if not self.new_kh.exists():
            return []
        return self.new_kh.read_text(encoding="utf-8").splitlines()

    # --- 読めない行を捨てないこと ---

    def test_a_short_line_survives_the_import(self):
        """3 欄未満の行も、そのまま引き継ぐこと。"""
        lines = self._import([SHORT_LINE, self._line("192.0.2.20",
                                                     self.key_a)])

        self.assertIn(
            SHORT_LINE, lines,
            "鍵欄の欠けた行が黙って捨てられている: %r" % (lines,))

    def test_a_carried_over_short_line_still_refuses_its_host(self):
        """引き継いだあとも、その行が指す接続先への接続を断ること。"""
        from core.ssh_connection import HostKeyStoreError, SSHConnection
        self._import([SHORT_LINE, self._line("192.0.2.20", self.key_a)])

        conn = SSHConnection(host="127.0.0.1", port=22, username="admin")
        self.addCleanup(conn.deleteLater)
        import paramiko
        client = paramiko.SSHClient()
        self.addCleanup(client.close)
        with self.assertRaises(HostKeyStoreError) as caught:
            conn._setup_host_keys(client)

        self.assertIn(
            SHORT_LINE, str(caught.exception),
            "どの行が読めないのかを知らせていない: %s" % caught.exception)

    def test_a_short_comment_survives_the_import(self):
        """2 語のコメント行も、そのまま引き継ぐこと。"""
        lines = self._import([COMMENT_LINE,
                              self._line("192.0.2.20", self.key_a)])

        self.assertIn(COMMENT_LINE, lines,
                      "コメント行が黙って捨てられている: %r" % (lines,))

    # --- 印付きの行の見分け ---

    def test_two_revoked_lines_for_one_host_are_both_kept(self):
        """同じホストの @revoked 行が鍵種別ごとにあれば、両方残ること。

        印を剥がさないと見分けが (印, ホスト) の 2 つになり、鍵種別が
        違っても同じ行と見なされて、旧ファイル側の失効指定が消える。
        """
        already = self._marked("@revoked", "192.0.2.30", self.key_a)
        other_type = self._marked("@revoked", "192.0.2.30", self.key_rsa)

        lines = self._import([other_type], new_lines=[already])

        self.assertIn(already, lines, "新しい側の失効指定が消えている")
        self.assertIn(
            other_type, lines,
            "鍵種別の違う失効指定が重複と見なされて捨てられている: %r"
            % (lines,))

    def test_a_revoked_line_is_not_a_duplicate_of_a_plain_line(self):
        """印の有無が違う行は、同じ行として扱わないこと。"""
        plain = self._line("192.0.2.30", self.key_a)
        revoked = self._marked("@revoked", "192.0.2.30", self.key_a)

        lines = self._import([revoked], new_lines=[plain])

        self.assertIn(plain, lines, "新しい側の行が消えている")
        self.assertIn(revoked, lines, "失効指定が引き継がれていない")

    # --- 今までどおりであること（対照） ---

    def test_a_valid_line_is_still_imported(self):
        """正常な行は、これまでどおり引き継ぐこと。"""
        valid = self._line("192.0.2.20", self.key_a)

        lines = self._import([SHORT_LINE, valid])

        self.assertIn(valid, lines)

    def test_a_line_already_present_is_not_imported_twice(self):
        """新しい側に同じ (ホスト, 鍵種別) があれば、重ねて書かないこと。"""
        new_line = self._line("192.0.2.20", self.key_b)
        old_line = self._line("192.0.2.20", self.key_a)

        lines = self._import([old_line], new_lines=[new_line])

        self.assertEqual(lines, [new_line],
                         "同じ接続先の行を重ねて書いている: %r" % (lines,))

    def test_the_marker_is_written_after_the_import(self):
        """引き継ぎ済みの目印は、これまでどおり作ること。"""
        self._import([SHORT_LINE])

        self.assertTrue(
            (self.new_dir / config_manager._IMPORT_MARKER_NAME).exists(),
            "引き継ぎ済みの目印が作られていない")

    def test_blank_lines_are_not_carried_over(self):
        """空行は持ち越さないこと（情報を持たない）。"""
        lines = self._import(["", self._line("192.0.2.20", self.key_a), ""])

        self.assertEqual(
            lines, [self._line("192.0.2.20", self.key_a)],
            "空行まで書き出している: %r" % (lines,))


if __name__ == "__main__":
    unittest.main()
