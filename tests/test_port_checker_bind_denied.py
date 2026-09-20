"""bind のアクセス拒否を、占有と断定しないことを検証する。

何が起きていたか（実測）:
  bind が WSAEACCES（errno 13 / WinError 10013）で断られたとき、10048
  （WSAEADDRINUSE）と同じ枝に入れて「既に使用されています／別のプログラムが
  このポートを使用中です」と断定し、元の OSError は捨てていた。10013 は占有
  以外（OS の予約・除外範囲、Hyper-V / WSL の予約）でも出るので、同じ画面の
  netstat 欄と矛盾する。bind だけを OSError(13, …, winerror=10013) に差し替えて
  PortCheckThread(50010, 'all', 'TCP') を走らせた実測:
    ✗ ポート 50010/TCP は既に使用されています
      → 別のプログラムがこのポートを使用中です
      → 下記のプロセス情報を確認してください
    …
    ポート 50010/TCP を使用している接続は見つかりませんでした
    → このポートは現在使用されていません
  同じ結果の中に「使用中」と「使用されていません」が並び、利用者には手掛かりが
  残らない。

  実機で本物の除外ポートを作るのは管理者権限でのシステム設定変更になるので、
  この検査でも bind だけを差し替えて同じ OSError を起こす。排他 bind に変えた
  あとは、実在の占有は TCP / UDP・SO_REUSEADDR の有無によらず 10048 になる
  ことを実測で確かめてある（この検査の control も本物の占有で見る）。

どう直したか:
  10048 と 13 / 10013 を分ける。10048 はこれまでどおり「既に使用されています」。
  13 / 10013 は占有と OS の予約・除外の両方が残る文にし、元の OSError の文字列と、
  除外範囲の確認手段（netsh int ipv4 show excludedportrange）を添える。
"""
import os
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

WINDOWS = sys.platform == "win32"
PORT = 50010


def run_check(port, check_type, protocol):
    """PortCheckThread を同じスレッドで走らせ、結果文字列を返す。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from ui.port_checker_gui import PortCheckThread
    out = []
    t = PortCheckThread(port, check_type, protocol)
    t.result_ready.connect(out.append)
    t.run()
    return "".join(out)


class PortCheckerBindDeniedTest(unittest.TestCase):
    def _denied(self, protocol="TCP", errno=13, winerror=10013):
        message = ("アクセス許可で禁じられている方法で"
                   "ソケットにアクセスしようとしました。")

        def bind(self, addr):
            e = OSError(errno, message)
            e.winerror = winerror
            raise e

        with mock.patch.object(socket.socket, "bind", bind):
            return run_check(PORT, "bind", protocol), message

    def test_an_access_denied_bind_is_not_called_an_occupied_port(self):
        """アクセス拒否を「別のプログラムが使用中」と断定しないこと。"""
        result, _ = self._denied()

        self.assertNotIn("既に使用されています", result, result)
        self.assertNotIn("別のプログラムがこのポートを使用中です", result, result)
        self.assertNotIn("バインド可能", result, result)

    def test_an_access_denied_bind_leaves_both_possibilities_open(self):
        """占有と、OS の予約・除外の両方が残る文にすること。"""
        result, _ = self._denied()

        self.assertIn("使用中", result, "占有の可能性が消えている: " + result)
        self.assertIn("予約", result, "OS の予約・除外の可能性が伝わらない: " + result)
        self.assertIn("excludedportrange", result,
                      "除外範囲の確認手段を案内していない: " + result)

    def test_the_original_error_is_not_swallowed(self):
        """元の OSError の文字列を捨てないこと。"""
        result, message = self._denied()

        self.assertIn(message, result, "元のエラーが残っていない: " + result)

    def test_a_plain_eacces_without_a_winerror_is_handled_the_same(self):
        """winerror の無い errno 13 でも、同じ扱いにすること。"""
        result, _ = self._denied(winerror=None)

        self.assertNotIn("既に使用されています", result, result)
        self.assertIn("予約", result, result)

    def test_an_unrelated_error_is_still_a_generic_one(self):
        """関係のないエラーは、これまでどおり汎用の表示のままであること。"""
        result, _ = self._denied(errno=99, winerror=None)

        self.assertIn("✗ エラー:", result, result)
        self.assertNotIn("excludedportrange", result, result)

    @unittest.skipUnless(WINDOWS, "Windows の排他 bind の挙動を見る")
    def test_a_real_holder_is_still_reported_as_in_use(self):
        """本物の占有（10048）は、これまでどおり「既に使用されています」であること。"""
        for protocol, kind in (("TCP", socket.SOCK_STREAM),
                               ("UDP", socket.SOCK_DGRAM)):
            with self.subTest(protocol=protocol):
                holder = socket.socket(socket.AF_INET, kind)
                self.addCleanup(holder.close)
                holder.bind(("", 0))
                if protocol == "TCP":
                    holder.listen(1)
                port = holder.getsockname()[1]

                result = run_check(port, "bind", protocol)

                self.assertIn(f"ポート {port}/{protocol} は既に使用されています",
                              result, result)
                self.assertNotIn("excludedportrange", result, result)


if __name__ == "__main__":
    unittest.main()
