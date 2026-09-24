"""ソケットの共通設定（待ち受けの排他と、接続先ポート番号の読み方）。"""
import socket
import sys


def set_exclusive_bind(sock) -> None:
    """他プロセスが使用中のポートを奪えないようにする。

    Windows の SO_REUSEADDR は Unix と意味が異なり、**既に待ち受けている
    ポートへの二重バインドを許してしまう**。そのため、他のツール（別の
    Syslog / TFTP サーバや OpenSSH Server など）が使っているポートへ
    黙って割り込め、到着したパケットがどちらへ渡るかが不定になる。
    「Syslog が半分しか出ない」「トラップが飛んでこない」といった、
    原因を追いにくい症状につながる。

    SO_EXCLUSIVEADDRUSE を立てると二重バインドが拒否され、ポートの衝突は
    起動時のエラーとして表面化する。Windows 以外では何もしない
    （Unix の SO_REUSEADDR は TIME_WAIT の再利用という別の意味を持つため）。

    なお SO_REUSEADDR が既に立っている場合は先に解除する（Windows では
    両方を同時に立てられないため）。
    """
    if sys.platform != "win32":
        return
    opt = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if opt is None:
        return

    # SO_REUSEADDR が既に立っていると SO_EXCLUSIVEADDRUSE の設定が
    # WinError 10022 で失敗する。pysnmp は自前のトランスポートソケットへ
    # 無条件にこれを立てるため、先に戻しておく。戻せなければ排他バインドも
    # 必ず失敗するので、同じ原因で二重に警告せずここで打ち切る。
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    except OSError as e:
        print(f"[Socket] SO_REUSEADDR を解除できませんでした: {e}"
              " (このポートは他プロセスに奪われる可能性があります)")
        return

    try:
        sock.setsockopt(socket.SOL_SOCKET, opt, 1)
    except OSError as e:
        # 立てられなくても待ち受け自体は続行できるが、ポートを奪われうる状態に
        # なるため黙って通さない。まず起きないが、起きたら追えるようにする。
        print(f"[Socket] SO_EXCLUSIVEADDRUSE を設定できませんでした: {e}"
              " (このポートは他プロセスに奪われる可能性があります)")


def tcp_port_number(port):
    """設定のポート番号を、接続に使う整数（1〜65535）にする。使えなければ None。

    手編集の config.json などでは "23" のように文字列になっていることがある。
    前後の空白を許す整数の文字列と、端数の無い数は整数へそろえる。
    bool（JSON の true）と端数のある数は、ポート番号として読まない
    （True は 1 番として通ってしまう）。SSH と Telnet の接続が同じ読み方を
    使う。
    """
    if isinstance(port, str):
        try:
            port = int(port)
        except ValueError:
            return None
    elif isinstance(port, float) and port.is_integer():
        port = int(port)
    if isinstance(port, bool) or not isinstance(port, int):
        return None
    return port if 1 <= port <= 65535 else None
