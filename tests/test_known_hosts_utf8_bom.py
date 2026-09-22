"""先頭に UTF-8 BOM が付いた known_hosts で、1 行目の機器だけ黙って未知に戻る件。

実測（a726deb / paramiko 4.0.0 / Windows・cp932 環境。
locale.getpreferredencoding(False) → 'cp932'、sys.flags.utf8_mode → 0）:
メモ帳の「UTF-8 (BOM)」や PowerShell 5.1 の Out-File -Encoding utf8 で
known_hosts を編集すると、先頭に BOM（EF BB BF）が付く。この状態で
_setup_host_keys() を通すと、1 行目の機器名が BOM ごと取り込まれて
'<BOM>192.0.2.8' になり、lookup('192.0.2.8') が None を返す。

  読み込んだ名前 = ['192.0.2.9', '\\ufeff192.0.2.8']
  lookup('192.0.2.8') = None、「読めない行」の警告は 0 件

つまり、その機器だけが警告なしに「未知のホスト」へ戻り、TOFU が別の鍵を
何も聞かずに受け入れる（実測: 別の鍵で missing_host_key を呼んでも
HostKeyMismatchError にならない）。さらに保存も毎回失敗する:

  known_hosts を保存できません（'cp932' codec can't encode character
  '\\ufeff' in position 0: illegal multibyte sequence）

ので新しい鍵が一度も残らず、次回もまた未知のままになる。
1 つ前（429bf80）では BOM 付きファイルは大きい音で全機器を中止していた
（'cp932' codec can't decode byte 0xef in position 0）ので、黙って未知へ
戻るのは 10 周目までの修正が持ち込んだ回帰。

locale が BOM を読めてしまう環境では、もっと悪くなる。PYTHONUTF8=1 で
同じ実測をすると paramiko の読み込み自体は成功し、やはり 1 行目の名前が
'<BOM>192.0.2.8' のまま登録されたうえ、TOFU が受け入れた別の鍵が
そのまま保存される（save_errs=[]、別鍵 saved=True）。

直し方:
  1. _iter_known_hosts_lines() で、読んだバイト列の先頭 BOM を剥がして
     から行に分ける。読めない行の点検・自前ローダ・保存時の書き戻しの
     3 経路すべてに同時に効く。
  2. BOM があるファイルは paramiko の HostKeys.load に渡さない
     （paramiko は BOM を剥がさないので、読めてしまう locale では 1 の
     修正が効かない）。壊れた行があるときと同じ自前ローダへ回す。
  3. その分岐でも client._host_keys_filename を None に戻す。paramiko の
     save_host_keys は保存前に _host_keys_filename を読み直すので、
     戻さないと同じファイルでまた落ちる（下の回帰テスト参照）。
"""
import codecs
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

# Write/Edit で書くと実文字になってしまうので、数値から組み立てる
BOM_CHAR = chr(0xFEFF)


class KnownHostsUtf8BomTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import paramiko
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()
        cls.key_c = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khbom-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.dir / "known_hosts"

    def _line(self, hostname, key, annotation=""):
        text = "%s %s %s" % (hostname, key.get_name(), key.get_base64())
        return text + (" " + annotation if annotation else "")

    def _write_known_hosts(self, lines, bom=True):
        data = ("\n".join(lines) + "\n").encode("utf-8")
        if bom:
            # メモ帳の「UTF-8 (BOM)」/ PowerShell 5.1 の Out-File が付ける
            data = codecs.BOM_UTF8 + data
        self.path.write_bytes(data)

    def _setup(self, host, port=22):
        """本物の SSHClient に _setup_host_keys を通し、画面に出た文を返す。"""
        import paramiko
        from core.ssh_connection import SSHConnection
        conn = SSHConnection(host=host, port=port, username="admin")
        self.addCleanup(conn.deleteLater)
        messages = []
        conn.output_received.connect(messages.append)
        client = paramiko.SSHClient()
        self.addCleanup(client.close)
        conn._setup_host_keys(client)
        return client, messages

    def test_bom_prefixed_host_stays_known(self):
        """BOM 付きでも 1 行目の機器が「未知のホスト」に戻らないこと。"""
        self._write_known_hosts([self._line("192.0.2.8", self.key_a),
                                 self._line("192.0.2.9", self.key_c)])

        client, messages = self._setup("192.0.2.8")

        names = sorted(client.get_host_keys().keys())
        self.assertNotIn(BOM_CHAR + "192.0.2.8", names,
                         "1 行目のホスト名に BOM が付いたまま: %r" % (names,))
        self.assertTrue(
            client.get_host_keys().check("192.0.2.8", self.key_a),
            "BOM 付きファイルの 1 行目の機器が未知に戻っている: %r" % (names,))
        self.assertEqual(
            [], [m for m in messages if "読めない行" in m],
            "正しい行を「読めない行」と呼んでいる: %r" % (messages,))

    def test_bom_prefixed_host_refuses_a_different_key(self):
        """BOM 付きでも、その機器の別の鍵を黙って受け入れないこと。"""
        from core.ssh_connection import (HostKeyMismatchError,
                                         _TofuHostKeyPolicy)
        self._write_known_hosts([self._line("192.0.2.8", self.key_a)])

        client, _messages = self._setup("192.0.2.8")
        policy = _TofuHostKeyPolicy(self.path)
        policy._on_save_error = lambda message: None

        with self.assertRaises(
                HostKeyMismatchError,
                msg="BOM 付きの 1 行目の機器が未知扱いになり、"
                    "別の鍵が確認なしで受け入れられている"):
            policy.missing_host_key(client, "192.0.2.8", self.key_b)

        self.assertIn(self.key_a.get_base64().encode("ascii"),
                      self.path.read_bytes(),
                      "保存済みの鍵が別の鍵で上書きされている")

    def test_tofu_save_succeeds_and_drops_the_bom(self):
        """BOM 付きファイルでも初回接続の鍵が保存でき、BOM が残らないこと。"""
        from core.ssh_connection import _TofuHostKeyPolicy
        self._write_known_hosts([self._line("192.0.2.8", self.key_a)])

        client, _messages = self._setup("192.0.2.5")
        policy = _TofuHostKeyPolicy(self.path)
        save_errors = []
        policy._on_save_error = save_errors.append
        policy.missing_host_key(client, "192.0.2.5", self.key_b)

        saved = self.path.read_bytes()
        self.assertEqual([], save_errors,
                         "BOM 付きファイルへの保存が失敗している: %r"
                         % (save_errors,))
        self.assertIn(self.key_b.get_base64().encode("ascii"), saved,
                      "初回接続の鍵が保存されていない")
        self.assertIn(self.key_a.get_base64().encode("ascii"), saved,
                      "元からあった鍵が保存で消えている")
        self.assertFalse(saved.startswith(codecs.BOM_UTF8),
                         "保存後のファイルに BOM が残っている")
        self.assertNotIn(BOM_CHAR.encode("utf-8"), saved,
                         "保存後のファイルに見えない文字が残っている")

    def test_bom_file_is_not_handed_to_paramikos_loader(self):
        """BOM 付きファイルは paramiko の読み込みに渡さないこと。

        paramiko は BOM を剥がさないので、BOM を読めてしまう locale
        （PYTHONUTF8=1 や cp1252）では paramiko の読み込みが成功し、
        1 行目の名前が BOM 付きのまま登録されてしまう（実測）。
        cp932 では UnicodeDecodeError で自前ローダに落ちるため、この
        振る舞いは _host_keys_filename でしか見分けられない。
        """
        self._write_known_hosts([self._line("192.0.2.8", self.key_a)])

        client, _messages = self._setup("192.0.2.8")

        self.assertIsNone(
            client._host_keys_filename,
            "BOM 付きファイルを paramiko に読ませている"
            "（BOM を読める locale では 1 行目が未知に戻る）")

    def test_no_bom_file_is_still_read_by_paramiko(self):
        """BOM が無ければ今までどおり paramiko に読ませること。"""
        self._write_known_hosts([self._line("192.0.2.8", self.key_a)],
                                bom=False)

        client, _messages = self._setup("192.0.2.8")

        self.assertEqual(str(self.path), client._host_keys_filename,
                         "BOM の無いファイルの読み方まで変わっている")
        self.assertTrue(client.get_host_keys().check("192.0.2.8", self.key_a))

    def test_save_survives_a_broken_line_added_after_setup(self):
        """setup のあとに壊れた行が足されても、初回接続の鍵が保存できること。

        実測（a726deb）: 読み込み時に壊れた行が無ければ paramiko が
        _host_keys_filename を覚える。そのあと別の NetBelt や利用者が
        壊れた行を足すと、save_host_keys が保存前にそのファイルを読み
        直して InvalidHostKey で落ち、

          known_hosts を保存できません（('192.0.2.7 ssh-rsa
          !!!not-base64!!!', Error('Invalid base64-encoded string: ...'))）

        となって、この機器の鍵が一度も残らない（次回もまた未知）。
        """
        from core.ssh_connection import _TofuHostKeyPolicy
        self._write_known_hosts([self._line("192.0.2.8", self.key_a)],
                                bom=False)

        client, _messages = self._setup("192.0.2.5")
        with open(self.path, "ab") as f:
            f.write(b"192.0.2.7 ssh-rsa !!!not-base64!!!\n")

        policy = _TofuHostKeyPolicy(self.path)
        save_errors = []
        policy._on_save_error = save_errors.append
        policy.missing_host_key(client, "192.0.2.5", self.key_b)

        saved = self.path.read_bytes()
        self.assertEqual([], save_errors,
                         "壊れた行のせいで保存そのものが失敗している: %r"
                         % (save_errors,))
        self.assertIn(self.key_b.get_base64().encode("ascii"), saved,
                      "初回接続の鍵が保存されていない")
        self.assertIn(self.key_a.get_base64().encode("ascii"), saved,
                      "元からあった鍵が保存で消えている")
        self.assertIn(b"192.0.2.7 ssh-rsa !!!not-base64!!!", saved,
                      "利用者が直すはずの壊れた行が消えている")


if __name__ == "__main__":
    unittest.main()
