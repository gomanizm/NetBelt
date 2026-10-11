"""SFTP クライアントのファイル一覧を更新し続けても、ヒープが増えないことを検証する。

何が起きていたか（9fee4af で実測）: _update_file_list は 1 行ごとに
self.model.appendRow([名前, サイズ, パーミッション, 更新日時]) としていた。
Trap の一覧（test_snmp_trap_display_heap_steady）と同じく、PyQt6 はこの
リスト引数（/Transfer/ 付きの QList<QStandardItem *>）の変換で new した
QList を解放しない。一覧を更新するたびに 1 行あたり 2 ブロック残り、
50 件の一覧では 1 回 100 ブロック・約 4.4 KB ずつ増え続けた（500 回で
50,000 ブロック・約 2.2 MB）。行を消しても、パネルを閉じて捨てても戻らない。
直した後は、同じ手順で 500 回更新しても 0 ブロック。

数え方: Windows のヒープの使用中のブロックの数（HeapWalk。数え方の関数は
test_snmp_trap_display_heap_steady のものを使う）。決まった数だけ増えるので
RSS のようには揺れない。
"""
import gc
import os
import sys
import unittest

sys.path.insert(0, "src")

from test_snmp_trap_display_heap_steady import _busy_heap_blocks  # noqa: E402

# 1 回の一覧の件数。漏れていれば 1 回の更新で 2 × ENTRIES ブロック増える
ENTRIES = 50
# 増え続けないことを、区切りごとに確かめる（区切り ROUNDS 個 × PER_ROUND 回）。
# 漏れていれば 1 区切りで 2 × 50 × 40 = 4000 ブロック増える
ROUNDS = 4
PER_ROUND = 40
# 揺れの許し幅。漏れていれば 1 区切りで 4000、画面を捨てる試験で 1 回 3000
# 増える。直した後に増えるのは、数える側の確保の 4〜6 と、測っている間に
# OS がスレッドを起こした分（1 本で 41。プロセスのスレッドの数と一致した。
# 最初のテストとして流すと 6 回に 2 回ほど 1〜2 本起きる）だけ
SLACK = 200


def _entries(n=ENTRIES):
    """ディレクトリ 5 件とファイルからなる一覧（SFTPManager が渡す形）"""
    out = []
    for i in range(n):
        is_dir = i < 5
        out.append({"name": ("dir%02d" % i) if is_dir else ("file-%03d.cfg" % i),
                    "size": 0 if is_dir else 1000 + 37 * i,
                    "mtime": 1700000000 + 60 * i,
                    "mode": 0o040755 if is_dir else 0o100644,
                    "is_dir": is_dir, "is_link": False,
                    "permissions": "drwxr-xr-x" if is_dir else "-rw-r--r--"})
    return out


class _Manager:
    """一覧の表示に要る分だけの相手（Mock は呼び出しを記録して増えるので使わない）"""
    is_connected = True

    def get_current_path(self):
        return "/home/user"


@unittest.skipUnless(sys.platform == "win32", "HeapWalk は Windows だけ")
class SftpFileListHeapSteadyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _flush(self):
        """配送待ちの知らせと、deleteLater した物の破棄を済ませる

        2 回まわす。破棄の途中で deleteLater された物（シグナルの中継など）は、
        同じ回では消えない。
        """
        from PyQt6.QtCore import QCoreApplication, QEvent
        for _ in range(2):
            for _ in range(3):
                self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def _panel(self):
        from ui.sftp_panel import SFTPPanel
        panel = SFTPPanel()
        panel.resize(700, 500)
        panel.show()
        panel.sftp_manager = _Manager()
        return panel

    def _dispose(self, panel):
        panel.sftp_manager = None
        panel.close()
        panel.deleteLater()
        self._flush()

    def test_refreshing_the_listing_does_not_keep_growing(self):
        """一覧を何度更新しても、区切りごとに増え続けないこと（傾きが 0）。"""
        panel = self._panel()
        self.addCleanup(self._dispose, panel)
        listing = _entries()
        # 一度きりの確保（文字の描画のキャッシュなど）を済ませておく
        for _ in range(10):
            panel._update_file_list(listing)
            self.app.processEvents()
        gc.collect()
        counts = [_busy_heap_blocks()]
        for _ in range(ROUNDS):
            for _ in range(PER_ROUND):
                panel._update_file_list(listing)
                self.app.processEvents()
            gc.collect()
            counts.append(_busy_heap_blocks())
        self.assertEqual(panel.model.rowCount(), ENTRIES)
        steps = [b - a for a, b in zip(counts, counts[1:])]
        # 漏れはどの区切りでも同じだけ増える。OS のスレッドの分は 1 区切りまで許す
        self.assertLessEqual(
            sum(step >= SLACK for step in steps), 1,
            "一覧を %d 回更新するたびに使用中のヒープブロックが増え続ける: %s"
            "（1 回あたり %.1f。行を置くたびに C++ 側に残っている）"
            % (PER_ROUND, steps, sorted(steps)[1] / PER_ROUND))

    def _cycle(self, refreshes):
        """パネルを作って一覧を refreshes 回更新し、閉じて捨てる"""
        from PyQt6 import sip
        listing = _entries()
        panel = self._panel()
        try:
            for _ in range(refreshes):
                panel._update_file_list(listing)
                self.app.processEvents()
            self.assertEqual(panel.model.rowCount(), ENTRIES)
        finally:
            self._dispose(panel)
        self.assertTrue(sip.isdeleted(panel), "前提: パネルが破棄されていない")
        del panel
        gc.collect()

    def test_closing_the_panel_gives_the_memory_back(self):
        """行を並べたパネルを閉じて捨てると、作る前のブロック数へ戻ること。"""
        # パネルを作るときの一度きりの確保を済ませる
        for _ in range(2):
            self._cycle(3)
        grown = []
        # 漏れは毎回同じだけ残る。OS のスレッドの分を避けるため 3 回測って小さい方
        for _ in range(3):
            before = _busy_heap_blocks()
            self._cycle(30)
            grown.append(_busy_heap_blocks() - before)
        self.assertLess(
            min(grown), SLACK,
            "行を並べたパネルを閉じて捨てても、使用中のヒープブロックが残った: %s"
            "（一覧の更新で C++ 側に残った分は、パネルを捨てても戻らない）" % grown)


if __name__ == "__main__":
    unittest.main()
