"""Trap の一覧が上限に達したあとは、表示へ足し続けてもヒープが増えないことを検証する。

何が起きていたか（9fee4af で実測）: 一覧が max_traps（既定 1000）に達して
古い行を捨てるようになったあとも、プロセスのメモリが Trap 1 件ごとに増え
続けた（子 4 行で約 0.6 KB、子 28 行で約 3.3 KB。毎秒 1 件で 1 日に数十〜
数百 MB）。gc の対象数・QStandardItem のラッパーの数・tracemalloc の値は
変わらず、増えていたのは C++ のヒープだった。

原因: PyQt6 の QStandardItemModel.insertRow / QStandardItem.appendRow に
Python のリストを渡す形。このリスト引数（QList<QStandardItem *>）は
/Transfer/ 付きで、PyQt6 は変換のたびに new した QList を「所有権を渡した」
扱いにして解放しない（qpycore_qlist.sip の %ConvertToTypeCode が
sipGetState(sipTransferObj) を返す）。1 回の呼び出しで QList 本体と中身の
配列の 2 ブロックが残る。空のリストでも残り、項目 1 つを渡す形
（insertRow(row, item) / setChild / setItem）では残らない。項目を Python で
作るか C++ で作るか、消すのが removeRow か takeRow かは関係しなかった。

数え方: RSS は揺れるので、Windows のヒープを HeapWalk でなめて使用中の
ブロックの数を数える（決まった数だけ増えるので揺れない）。漏れていれば
Trap 1 件あたり 2 ×（親 1 + 子の行数）ブロック増える。
"""
import ctypes
import gc
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

# 上限に達したあとに足す件数。漏れていれば 2 × (1 + 4) × FEED ブロック増える
FEED = 300
# 揺れの許し幅（漏れていれば 3000 増える。直した後の実測は 4〜6 で、何も
# 足さずに続けて 2 回数えても同じだけ増えるので、数える側の確保の分）
SLACK = 60


def _trap(n, extra=4):
    varbinds = [
        {"oid": "1.3.6.1.2.1.1.3.0", "value": "12345"},
        {"oid": "1.3.6.1.6.3.1.1.4.1.0", "value": "1.3.6.1.6.3.1.1.5.3"},
    ]
    varbinds += [{"oid": "1.3.6.1.2.1.2.2.1.%d.%d" % (k + 1, n % 7),
                  "value": "if-%05d-%d" % (n, k)} for k in range(extra)]
    return {
        "source_ip": "192.0.2.%d" % (n % 250 + 1),
        "source_port": 162,
        "trap_oid": "1.3.6.1.6.3.1.1.5.3",
        "received_at": "2026-10-03T10:00:%02d.000000" % (n % 60),
        "security_model": "2",
        "varbinds": varbinds,
    }


def _busy_heap_blocks():
    """プロセスの全ヒープの、使用中のブロックの数（Windows の HeapWalk）

    走査の間は GIL を手放さない。HeapLock を持ったまま GIL が他の Python
    スレッドへ渡ると、そのスレッドが GIL を持ったまま同じヒープの確保
    （malloc）で錠を待ち、こちらは GIL を待って、互いに止まる（全件を
    1 プロセスで流し、前のテストのスレッドが残っているときに起きうる）。
    - PyDLL で呼ぶ（WinDLL は呼び出しのたびに GIL を手放す）
    - PyDLL でもループの折り返しで GIL を譲るので、切り替え間隔を走査より
      十分長い 10 秒に延ばす（実測: 100 万ブロックの走査で 0.3〜2.3 秒）。
      延ばす前から待っていたスレッドは古い間隔で譲れと言ってくるので、
      一度 sleep で手放して受け取り直し、待ち手を新しい間隔にしてから錠を取る
      （CPU を使い続ける Python スレッドが残っていると、受け取り直しで
      その数 × 約 10 秒待つ。止まったままになるよりはよい）
    - 終了処理（__del__）で GIL を手放さないよう、gc も止める
    """
    from ctypes import wintypes

    class PROCESS_HEAP_ENTRY(ctypes.Structure):
        _fields_ = [("lpData", ctypes.c_void_p), ("cbData", wintypes.DWORD),
                    ("cbOverhead", ctypes.c_ubyte),
                    ("iRegionIndex", ctypes.c_ubyte),
                    ("wFlags", wintypes.WORD), ("u", ctypes.c_ubyte * 24)]

    # windll.kernel32 の argtypes を書き換えると他のテストへ移るので自分用に読む
    k32 = ctypes.PyDLL("kernel32")
    k32.GetProcessHeaps.argtypes = [wintypes.DWORD,
                                    ctypes.POINTER(wintypes.HANDLE)]
    k32.GetProcessHeaps.restype = wintypes.DWORD
    k32.HeapWalk.argtypes = [wintypes.HANDLE,
                             ctypes.POINTER(PROCESS_HEAP_ENTRY)]
    k32.HeapWalk.restype = wintypes.BOOL
    k32.HeapLock.argtypes = [wintypes.HANDLE]
    k32.HeapUnlock.argtypes = [wintypes.HANDLE]
    busy = 0x0004   # PROCESS_HEAP_ENTRY_BUSY

    switch_interval = sys.getswitchinterval()
    gc_enabled = gc.isenabled()
    sys.setswitchinterval(10)
    gc.disable()
    try:
        time.sleep(0.001)
        count = k32.GetProcessHeaps(0, None)
        heaps = (wintypes.HANDLE * (count + 16))()
        count = k32.GetProcessHeaps(len(heaps), heaps)
        total = 0
        for i in range(count):
            if not k32.HeapLock(heaps[i]):
                continue
            try:
                entry = PROCESS_HEAP_ENTRY()
                while k32.HeapWalk(heaps[i], ctypes.byref(entry)):
                    if entry.wFlags & busy:
                        total += 1
            finally:
                k32.HeapUnlock(heaps[i])
    finally:
        sys.setswitchinterval(switch_interval)
        if gc_enabled:
            gc.enable()
    return total


class TrapDisplayHeapSteadyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 作ったウィンドウはクラス終了まで保持する（test_snmp_trap_limit と同じ）
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _panel(self, max_traps):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-trap-heap-")
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        cm.config.setdefault("settings", {})["snmp"] = {"max_traps": max_traps}
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = cm
            window = MainWindow()
        type(self)._windows.append(window)
        return window.snmp_panel

    @unittest.skipUnless(sys.platform == "win32", "HeapWalk は Windows だけ")
    def test_feeding_past_the_limit_leaves_no_heap_blocks_behind(self):
        panel = self._panel(20)
        # 上限まで埋め、MIB の名前解決などの一度きりの確保も済ませておく
        for n in range(200):
            panel._add_trap_to_tree(_trap(n))
        self.assertEqual(panel.trap_tree_model.rowCount(), 20)
        gc.collect()
        before = _busy_heap_blocks()
        for n in range(200, 200 + FEED):
            panel._add_trap_to_tree(_trap(n))
        gc.collect()
        grown = _busy_heap_blocks() - before
        self.assertEqual(panel.trap_tree_model.rowCount(), 20)
        self.assertLess(
            grown, SLACK,
            "上限に達したあと %d 件足したら、使用中のヒープブロックが %d 増えた"
            "（1 件あたり %.1f。表示へ入れるたびに C++ 側に残っている）"
            % (FEED, grown, grown / FEED))

    def test_the_tree_looks_the_same(self):
        """行・列・子の並びと中身が、直す前と同じであること。"""
        from core.mib_resolver import get_resolver
        name = get_resolver().resolve_oid
        panel = self._panel(20)
        panel._add_trap_to_tree(_trap(1, extra=3))
        panel._add_trap_to_tree(_trap(2, extra=0))
        model = panel.trap_tree_model
        self.assertEqual(model.rowCount(), 2)
        self.assertEqual(model.columnCount(), 5)

        # 先頭が新しい方。子の無い Trap は子も列も持たない（展開の印が出ない）
        newest = model.item(0, 0)
        self.assertEqual([model.item(0, c).text() for c in range(5)],
                         ["2026-10-03 10:00:02", "192.0.2.3", "v2c",
                          name("1.3.6.1.6.3.1.1.5.3"), "VarBindsなし"])
        self.assertEqual((newest.rowCount(), newest.columnCount()), (0, 0))
        self.assertFalse(model.hasChildren(newest.index()))

        older = model.item(1, 0)
        self.assertEqual([model.item(1, c).text() for c in range(5)],
                         ["2026-10-03 10:00:01", "192.0.2.2", "v2c",
                          name("1.3.6.1.6.3.1.1.5.3"), "3 VarBinds"])
        # sysUpTime と snmpTrapOID は子にしない。子は 1 行 5 列で、OID と値だけ
        self.assertEqual((older.rowCount(), older.columnCount()), (3, 5))
        self.assertTrue(model.hasChildren(older.index()))
        for k in range(3):
            self.assertEqual(
                [older.child(k, c).text() for c in range(5)],
                ["", "", "", name("1.3.6.1.2.1.2.2.1.%d.1" % (k + 1)),
                 "if-00001-%d" % k])
        # 子の行は第 0 列の項目の下にあり、他の列の項目は子を持たない
        for c in range(1, 5):
            self.assertEqual(model.item(1, c).rowCount(), 0)
        self.assertEqual(len(panel.trap_data_list), 2)


if __name__ == "__main__":
    unittest.main()
