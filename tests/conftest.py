"""テスト共通の設定。

このリポジトリのテストは QApplication と QObject（各サーバのマネージャや UI
パネル）を多数作る。PyQt では QApplication の Python 側参照が失われると、
残っているオブジェクトの後片付けで解放済みの C++ オブジェクトに触れ、
プロセスごと落ちることがある（実測で access violation）。

そのため QApplication はセッション全体で1つだけ作り、最後まで保持する。

Trap のバイト列を組み立てるヘルパもここに置く。複数のテストモジュールが
使うが、`from tests.xxx import` はリポジトリルートが sys.path に入る
起動方法（python -m pytest）でしか通らず、素の pytest では
ModuleNotFoundError になる。conftest は pytest が必ず import できる。
"""
import os
import socket
import sys
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# offscreen QPA はシステムのフォントを自動では拾わない（実測でフォントDB 0件）。
# QFontComboBox が項目を1つも持てず、currentFont() が "Sans Serif" に化けるため、
# フォントを扱うテストが原理的に検証不能になる。フォントディレクトリを教える。
# 存在しない環境（非Windows）では何もしない。
_font_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
if os.path.isdir(_font_dir):
    os.environ.setdefault("QT_QPA_FONTDIR", _font_dir)


@pytest.fixture(scope="session", autouse=True)
def qapp():
    """セッション全体で1つの QApplication を保持する。"""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])

    yield app

    # 終了前に残ったイベントを捌く
    for _ in range(3):
        app.processEvents()

    # 停止し損ねたスレッドがあれば知らせる（見逃すと終了時に落ちる原因になる）
    leftovers = [t for t in threading.enumerate()
                 if t is not threading.main_thread() and t.is_alive() and not t.daemon]
    if leftovers:
        print("\n[conftest] 終了時に非デーモンスレッドが残っています: %s"
              % ", ".join(t.name for t in leftovers))


@pytest.fixture(autouse=True)
def no_startup_update_check():
    """テスト中は起動時の更新チェックを走らせない。

    MainWindow() を組むと _check_for_updates_on_startup が走る。これは
    config.json の update_settings.check_on_startup（既定 True）だけを見て、
    バックグラウンドスレッドで api.github.com へ問い合わせ、トークンが
    あれば（環境変数 GITHUB_TOKEN か config.json）Authorization ヘッダに
    載せて送る。テストは MainWindow を何度も組むので、pytest を1回回す
    だけで外部への HTTPS が何度も飛ぶ。

    さらに _check_pending_updates は %TEMP% に検証済みで現行版より新しい
    ZIP があるとモーダルを出す。offscreen では閉じる相手がいないので、
    そういう端末ではテストが止まる。

    個々のテストが自分でパッチする形だと、書き忘れたファイルから漏れる
    （実際、複数のファイルで抜けていた）。ここで一律に止める。
    自分でパッチしているテストは、その上に重ねてかかるだけで影響しない。
    """
    import os
    from unittest import mock

    src = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from ui.main_window import MainWindow
    except Exception:
        # PyQt を使わないテストでは import できなくてよい
        yield
        return

    with mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
        yield


def free_udp_port():
    """空いている UDP ポートを1つ調べて返す。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _trap_bytes(proto_version, community):
    from pyasn1.codec.ber import encoder
    from pysnmp.proto import api

    pMod = api.protoModules[proto_version]
    pdu = pMod.TrapPDU()
    pMod.apiTrapPDU.setDefaults(pdu)
    msg = pMod.Message()
    pMod.apiMessage.setDefaults(msg)
    pMod.apiMessage.setCommunity(msg, community)
    pMod.apiMessage.setPDU(msg, pdu)
    return encoder.encode(msg)


def trap_bytes(community="public"):
    """指定したコミュニティを持つ SNMPv2c Trap のバイト列。"""
    from pysnmp.proto import api
    return _trap_bytes(api.protoVersion2c, community)


def v1_trap_bytes(community="public"):
    """指定したコミュニティを持つ SNMPv1 Trap のバイト列。"""
    from pysnmp.proto import api
    return _trap_bytes(api.protoVersion1, community)
