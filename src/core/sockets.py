"""待ち受けソケットの共通設定。"""
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
    """
    if sys.platform != "win32":
        return
    opt = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if opt is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, opt, 1)
    except OSError as e:
        # 立てられなくても待ち受け自体は続行できるが、ポートを奪われうる状態に
        # なるため黙って通さない。まず起きないが、起きたら追えるようにする。
        print(f"[Socket] SO_EXCLUSIVEADDRUSE を設定できませんでした: {e}"
              " (このポートは他プロセスに奪われる可能性があります)")
