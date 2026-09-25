"""旧 known_hosts の引き継ぎで、BOM がファイルの途中に入る件。

実測（979e82f / Windows・cp932 環境）: 11 周目で「先頭の BOM を剥がして
から読む」を入れたが、旧 ~/.terminal-tool/known_hosts を引き継ぐ経路
（_import_legacy_known_hosts_unlocked）は encoding="utf-8" で読むため、
旧ファイル 1 行目の BOM (U+FEFF) がホスト名に付いたまま新しい
~/.netbelt/known_hosts へ書かれる。新ファイルに既に行があると、その
BOM 付きの名前は「先頭」ではなく途中に来るので、11 周目の修正は効かない。

  旧 = BOM 付き 192.0.2.8 / 新 = 192.0.2.9 あり
  → 引き継ぎ後の名前 = ['192.0.2.9', '<BOM>192.0.2.8']
    lookup('192.0.2.8') は None で、「読めない行」の警告も 0 件。
    つまり 192.0.2.8 だけが黙って「未知」へ戻り、TOFU が別の鍵を
    何も聞かずに受け入れる。
  さらに以後の TOFU 保存は毎回
    known_hosts を保存できません（'cp932' codec can't encode
    character U+FEFF in position ...: illegal multibyte sequence）
  で失敗するので、新しい機器の鍵が一度も残らない。

新ファイル側に BOM がある場合（メモ帳や PowerShell 5.1 の Out-File で
編集した場合）も同じ読み方の問題で、既存の行が見分けられず、旧ファイルの
同じ機器の行が重ねて書かれる。paramiko は後の行を採るので、新ファイルに
あった新しい鍵が旧い鍵に上書きされてしまう。

起きる条件は狭い（引き継ぎ時に新ファイルへ既に行がある = 1 回目の引き継ぎ
が錠待ち等で失敗して再試行した場合や、使い始めたあとに旧 PC の
~/.terminal-tool を復元した場合）。v1.3.0 で引き継ぎ済みの利用者には
起きない。

直し方: _import_legacy_known_hosts_unlocked の read_text を、旧ファイル・
新ファイルとも encoding="utf-8-sig" にする。utf-8-sig は先頭の BOM を
剥がすだけで、ほかは utf-8 と同じ。
"""
import codecs
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

# Write/Edit で書くと実文字になってしまうので、数値から組み立てる
BOM_CHAR = chr(0xFEFF)


class LegacyKnownHostsImportBomTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import paramiko
        cls.key_old = paramiko.ECDSAKey.generate()
        cls.key_new = paramiko.ECDSAKey.generate()
        cls.key_other = paramiko.ECDSAKey.generate()

    def _line(self, host, key):
        return "%s %s %s" % (host, key.get_name(), key.get_base64())

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="netbelt-home-"))
        self.addCleanup(shutil.rmtree, str(self.home), True)
        (self.home / ".terminal-tool").mkdir()
        self.old_kh = self.home / ".terminal-tool" / "known_hosts"
        self.new_dir = self.home / ".netbelt"
        self.new_dir.mkdir()
        self.path = self.new_dir / "known_hosts"
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.new_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, path, lines, bom=False):
        body = "".join(line + "\n" for line in lines).encode("utf-8")
        path.write_bytes((codecs.BOM_UTF8 if bom else b"") + body)

    def _import(self):
        from core import config_manager
        with mock.patch("pathlib.Path.home", return_value=self.home):
            return config_manager._import_legacy_known_hosts_unlocked(
                self.new_dir)

    def _setup(self, host):
        """本番と同じ経路で known_hosts を読み込んだ SSHClient を返す。"""
        import paramiko
        from core.ssh_connection import SSHConnection
        conn = SSHConnection(host=host, port=22, username="admin")
        self.addCleanup(conn.deleteLater)
        client = paramiko.SSHClient()
        self.addCleanup(client.close)
        conn._setup_host_keys(client)
        return client

    def test_imported_bom_line_keeps_the_host_known(self):
        # 旧 = BOM 付き、新 = 別の機器が既にいる（引き継ぎの再試行を模す）
        self._write(self.old_kh, [self._line("192.0.2.8", self.key_old)],
                    bom=True)
        self._write(self.path, [self._line("192.0.2.9", self.key_other)])
        self.assertIsNone(self._import())
        self.assertNotIn(BOM_CHAR.encode("utf-8"), self.path.read_bytes(),
                         "引き継いだ行に見えない文字（BOM）が残っている")
        client = self._setup("192.0.2.8")
        self.assertTrue(
            client.get_host_keys().check("192.0.2.8", self.key_old),
            "引き継いだ 1 行目の機器が黙って未知へ戻っている: %r"
            % (sorted(client.get_host_keys().keys()),))

    def test_tofu_save_still_works_after_the_import(self):
        from core.ssh_connection import _TofuHostKeyPolicy
        self._write(self.old_kh, [self._line("192.0.2.8", self.key_old)],
                    bom=True)
        self._write(self.path, [self._line("192.0.2.9", self.key_other)])
        self._import()
        client = self._setup("192.0.2.5")
        policy = _TofuHostKeyPolicy(self.path)
        errors = []
        policy._on_save_error = errors.append
        fresh = self.key_new
        policy.missing_host_key(client, "192.0.2.5", fresh)
        self.assertEqual([], errors, "引き継ぎ後の保存が失敗している: %r"
                         % (errors,))
        saved = self.path.read_bytes()
        self.assertIn(fresh.get_base64().encode("ascii"), saved,
                      "新しい機器の鍵が残っていない")
        self.assertIn(self.key_old.get_base64().encode("ascii"), saved)
        self.assertIn(self.key_other.get_base64().encode("ascii"), saved)

    def test_bom_in_the_new_file_does_not_bring_back_the_old_key(self):
        # 新ファイルがメモ帳等で編集されて BOM 付きになっている場合。
        # 同じ機器の行を見分けられないと、旧い鍵が後ろに足されて
        # そちらが採られる（新しい鍵が黙って旧い鍵に戻る）
        self._write(self.old_kh, [self._line("192.0.2.8", self.key_old),
                                  self._line("192.0.2.7", self.key_other)])
        self._write(self.path, [self._line("192.0.2.8", self.key_new)],
                    bom=True)
        self.assertIsNone(self._import())
        client = self._setup("192.0.2.8")
        self.assertTrue(
            client.get_host_keys().check("192.0.2.8", self.key_new),
            "新ファイルにあった鍵が旧い鍵に上書きされている")
        self.assertFalse(
            client.get_host_keys().check("192.0.2.8", self.key_old),
            "引き継ぎが旧い鍵を足したので、古い鍵でも通ってしまう")


if __name__ == "__main__":
    unittest.main()
