"""Windows ファイアウォール受信許可の管理

NetBelt の受信サーバ（SNMP Trap / Syslog / SFTP サーバ）を起動する際、
その受信ポートに対する Windows Defender ファイアウォールの受信許可ルールを追加する。

- ルール追加には管理者権限が必要なため、未昇格時のみ ShellExecute の "runas" で
  netsh を昇格実行する（対象ポートのルールを初めて作る時だけ UAC が表示される。冪等）。
- ルールは "NetBelt - <サービス> (<PROTO>/<port>)" という名前で作成する。
- Windows 以外では何もしない（常に成功扱い）。
"""
import sys
import subprocess

_RULE_PREFIX = "NetBelt"
_CREATE_NO_WINDOW = 0x08000000  # コンソール窓のちらつき防止（Windowsのみ）


def is_windows():
    return sys.platform == "win32"


def is_admin():
    """現在のプロセスが管理者権限で動作しているか"""
    if not is_windows():
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def rule_name(service, proto, port):
    """ファイアウォールルール名を生成する"""
    return "{0} - {1} ({2}/{3})".format(_RULE_PREFIX, service, str(proto).upper(), port)


def _netsh(args):
    """netsh を通常権限で実行し CompletedProcess を返す（Windows専用・窓なし）"""
    # text 復号はしない（netsh の出力コードページで UnicodeDecodeError になり得るため）。
    # 判定には returncode のみ使う。
    return subprocess.run(
        ["netsh"] + args,
        capture_output=True,
        creationflags=_CREATE_NO_WINDOW,
    )


def rule_exists(name):
    """指定名の受信ルールが既に存在するか（読み取りのみ・管理者権限不要）"""
    if not is_windows():
        return False
    try:
        r = _netsh(["advfirewall", "firewall", "show", "rule", "name=" + name])
        # 存在しない場合 netsh は returncode!=0 で "No rules match..." を返す
        return r.returncode == 0
    except Exception:
        return False


def _add_rule_direct(name, proto, port):
    """管理者権限がある場合の直接追加"""
    r = _netsh([
        "advfirewall", "firewall", "add", "rule",
        "name=" + name, "dir=in", "action=allow",
        "protocol=" + str(proto).upper(), "localport=" + str(port),
        "profile=any", "enable=yes",
    ])
    return r.returncode == 0


def _add_rule_elevated(name, proto, port):
    """未昇格時: netsh を UAC 昇格で実行してルールを追加する。
    ShellExecuteW は昇格プロセスを起動できたか（>32）のみを返すため、
    実際の追加可否は呼び出し側で rule_exists により確認する。
    """
    import ctypes
    params = (
        'advfirewall firewall add rule name="{0}" '
        'dir=in action=allow protocol={1} localport={2} '
        'profile=any enable=yes'
    ).format(name, str(proto).upper(), port)
    # 第6引数 0 = SW_HIDE（窓を出さない）
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "netsh", params, None, 0)
    return int(rc) > 32


def ensure_inbound_allow(service, proto, port):
    """受信ポートの許可ルールを保証する。

    Returns:
        (ok: bool, message: str)
    """
    if not is_windows():
        return True, "非Windowsのためスキップ"

    name = rule_name(service, proto, port)
    try:
        if rule_exists(name):
            return True, "既存の許可ルールを使用: " + name

        if is_admin():
            if _add_rule_direct(name, proto, port):
                return True, "許可ルールを追加: " + name
            return False, "許可ルールの追加に失敗: " + name

        # 未昇格: UAC で昇格実行（初回のみプロンプト）
        if not _add_rule_elevated(name, proto, port):
            return False, "UACが承認されず未追加: " + name

        # 昇格プロセスは非同期のため、短時間だけ存在確認をリトライ
        import time
        for _ in range(6):
            time.sleep(0.25)
            if rule_exists(name):
                return True, "許可ルールを追加(管理者昇格): " + name
        return True, "許可ルールの追加を要求(反映待ち): " + name
    except Exception as e:
        return False, "ファイアウォール設定エラー: " + str(e)


def _self_program():
    """frozen(exe) のときのみ自身の実行体パスを返す。開発実行時は None。"""
    return sys.executable if getattr(sys, "frozen", False) else None


def _self_rule_name():
    return "{0} - app inbound (self)".format(_RULE_PREFIX)


def ensure_self_program_allow():
    """自 exe 宛の受信ブロックを削除し受信許可を追加する（ブロックは許可を上書きするため）。
    Windows かつ frozen のときのみ実行。冪等。未昇格なら UAC 昇格で netsh 実行。"""
    if not is_windows():
        return True, "非Windowsのためスキップ"
    prog = _self_program()
    if not prog:
        return True, "開発実行のためスキップ（frozen時のみ有効）"
    name = _self_rule_name()
    # netsh の program 指定ルールを、既存の add/delete と同じ昇格経路で処理する。
    # 未昇格時は ShellExecuteW で delete(block)+add(allow) をまとめて昇格実行する。
    try:
        if is_admin():
            _netsh(["advfirewall", "firewall", "delete", "rule", "name=all", "dir=in",
                    "action=block", 'program=' + prog])
            _netsh(["advfirewall", "firewall", "add", "rule", "name=" + name, "dir=in",
                    "action=allow", 'program=' + prog, "profile=any", "enable=yes"])
            return True, "自exe受信許可を保証: " + name
        # 未昇格: cmd.exe /c 経由で delete(block)+add(allow) を昇格実行（& をシェルに解釈させる）。
        # ShellExecuteW(lpFile="netsh") は netsh を直接起動するため & がシェル区切りにならず add が実行されない。
        import ctypes
        cmd = ('/c netsh advfirewall firewall delete rule name=all dir=in action=block '
               'program="{0}" & netsh advfirewall firewall add rule name="{1}" dir=in '
               'action=allow program="{0}" profile=any enable=yes').format(prog, name)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", cmd, None, 0)
        if int(rc) <= 32:
            return False, "UACが承認されず未設定: " + name
        # 昇格プロセスは非同期。allow ルールの反映を短時間リトライ確認する。
        import time
        for _ in range(6):
            time.sleep(0.25)
            if rule_exists(name):
                return True, "自exe受信許可を追加(昇格): " + name
        return True, "自exe受信許可を要求(反映待ち): " + name
    except Exception as e:
        return False, "自exe受信許可の設定エラー: " + str(e)
