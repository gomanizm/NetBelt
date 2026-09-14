"""Windows ファイアウォール受信許可の管理

NetBelt の受信サーバ（FTP / TFTP / Syslog / SFTP サーバ / SNMP Trap）の
受信ポートに対する Windows Defender ファイアウォールの受信許可ルールを追加する。

- v1.3.0 以降、待ち受け開始では呼ばない。受信できないときに利用者が
  各パネルの「ファイアウォールで許可（管理者）」を押したときだけ呼ぶ
  （tests/test_firewall_policy.py が起動時に触らないことを保証している）。
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


# netsh show rule の出力はロケール依存。日本語と英語のキー/値を受け付ける
_KEY_ENABLED = ("enabled", "有効")
_KEY_ACTION = ("action", "操作")
_VAL_YES = ("yes", "はい")
_VAL_NO = ("no", "いいえ")
_VAL_ALLOW = ("allow", "許可")
_VAL_BLOCK = ("block", "ブロック")


def _tri(val, true_vals, false_vals):
    """値を True / False / None（認識語彙に無い＝読めなかった）で返す"""
    if val in true_vals:
        return True
    if val in false_vals:
        return False
    return None


def _decode_netsh(data):
    """netsh の出力バイト列を文字列にする（この環境では UTF-8、他は cp932 等）"""
    for enc in ("utf-8", "mbcs"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1")


def _rule_state(name):
    """指定名の受信ルールの状態を返す。

    Returns:
        "ok"      … 有効かつ許可の受信ルールがある
        "present" … 同名の受信ルールはあるが無効またはブロック
        "absent"  … 無い
        "unknown" … 同名ルールはあるが出力を解釈できない（未知のロケール）

    「有効」と「操作」の両方を値まで読めたルールだけを判断材料にする。
    片方しか読めない（キーは英語と同綴りだが値が未知の語、など）出力を
    "present" にすると、実際は許可されているのに毎回 UAC で修復を求める。

    同名ルールは全件を読み終えてから判定する。Windows はブロックを許可より
    優先するため、有効なブロックが1件でもあれば受信は通らない。先に有効な
    許可を見つけた時点で "ok" を返すと、その後ろのブロックを見落とす。

    制限事項: プロファイル・プロトコル・ローカルポートは見ていない。
    同名でプロファイル限定・別ポートのルールがあると "ok" と判定する。
    ルール名にプロトコルとポートを含めているため実運用では一致するが、
    利用者が手で同名ルールを作り替えた場合は取りこぼす。
    """
    r = _netsh(["advfirewall", "firewall", "show", "rule", "name=" + name, "dir=in"])
    # 存在しない場合 netsh は returncode!=0 で "No rules match..." を返す
    if r.returncode != 0:
        return "absent"
    text = _decode_netsh(r.stdout or b"")
    # ルールごとに 有効/操作 を集める。区切り線（----）で次のルールへ移る
    rules = []  # 両方を値まで読めた (有効, 許可) の組
    enabled = allow = None
    for line in text.splitlines():
        if line.startswith("----"):
            if enabled is not None and allow is not None:
                rules.append((enabled, allow))
            enabled = allow = None
            continue
        key, sep, val = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        val = val.strip().lower()
        if key in _KEY_ENABLED:
            enabled = _tri(val, _VAL_YES, _VAL_NO)
        elif key in _KEY_ACTION:
            allow = _tri(val, _VAL_ALLOW, _VAL_BLOCK)
    if enabled is not None and allow is not None:
        rules.append((enabled, allow))
    if not rules:
        return "unknown"
    # 有効なブロックは有効な許可に優先する（修復対象）
    if any(en and not al for en, al in rules):
        return "present"
    if any(en and al for en, al in rules):
        return "ok"
    return "present"


def rule_exists(name):
    """指定名の「有効な許可」受信ルールが存在するか（読み取りのみ・管理者権限不要）

    名前が一致するだけでは真にしない。無効化・ブロック化された同名ルールは
    受信を通さないので、それを「許可済み」と報告すると通らないのに完了と出る。
    出力を解釈できないロケールでは従来どおり名前一致で真とする。
    """
    if not is_windows():
        return False
    try:
        return _rule_state(name) in ("ok", "unknown")
    except Exception:
        return False


def _rule_args(name, proto, port, repair):
    """add（新規）または set（同名ルールを有効・許可へ修復）の netsh 引数"""
    if repair:
        return ["advfirewall", "firewall", "set", "rule", "name=" + name, "dir=in",
                "new", "enable=yes", "action=allow"]
    return ["advfirewall", "firewall", "add", "rule",
            "name=" + name, "dir=in", "action=allow",
            "protocol=" + str(proto).upper(), "localport=" + str(port),
            "profile=any", "enable=yes"]


def _add_rule_direct(name, proto, port, repair=False):
    """管理者権限がある場合の直接追加（repair=True なら同名ルールの修復）"""
    r = _netsh(_rule_args(name, proto, port, repair))
    return r.returncode == 0


def _add_rule_elevated(name, proto, port, repair=False):
    """未昇格時: netsh を UAC 昇格で実行してルールを追加する。
    ShellExecuteW は昇格プロセスを起動できたか（>32）のみを返すため、
    実際の追加可否は呼び出し側で rule_exists により確認する。
    """
    import ctypes
    # 空白を含む値（ルール名）は key="value" の形で渡す
    params = " ".join(
        '%s="%s"' % tuple(a.split("=", 1)) if " " in a else a
        for a in _rule_args(name, proto, port, repair))
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
        state = _rule_state(name)
        if state in ("ok", "unknown"):
            return True, "既存の許可ルールを使用: " + name
        # 同名ルールが無効/ブロックなら add で二重化せず set で修復する
        repair = state == "present"
        verb = "修復" if repair else "追加"

        if is_admin():
            if _add_rule_direct(name, proto, port, repair):
                return True, "許可ルールを%s: %s" % (verb, name)
            return False, "許可ルールの%sに失敗: %s" % (verb, name)

        # 未昇格: UAC で昇格実行（初回のみプロンプト）
        if not _add_rule_elevated(name, proto, port, repair):
            return False, "UACが承認されず未%s: %s" % (verb, name)

        # 昇格プロセスは非同期のため、短時間だけ存在確認をリトライ
        import time
        for _ in range(6):
            time.sleep(0.25)
            if rule_exists(name):
                return True, "許可ルールを%s(管理者昇格): %s" % (verb, name)
        # 反映を確認できないものを成功にすると「通らないのに完了」と出る
        return False, "許可ルールの%sを要求したが反映を確認できず: %s" % (verb, name)
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
            # delete は該当ブロックが無ければ rc!=0 なので結果は見ない
            _netsh(["advfirewall", "firewall", "delete", "rule", "name=all", "dir=in",
                    "action=block", 'program=' + prog])
            r = _netsh(["advfirewall", "firewall", "add", "rule", "name=" + name, "dir=in",
                        "action=allow", 'program=' + prog, "profile=any", "enable=yes"])
            if r.returncode != 0:
                return False, "自exe受信許可の追加に失敗: " + name
            return True, "自exe受信許可を保証: " + name
        # 未昇格: cmd.exe /c 経由で delete(block)+add(allow) を昇格実行（& をシェルに解釈させる）。
        # ShellExecuteW(lpFile="netsh") は netsh を直接起動するため & がシェル区切りにならず add が実行されない。
        #
        # cmd.exe は二重引用符の中でも %VAR% を展開するので、置き場所の名前に
        # 定義済みの環境変数名が %..% の形で含まれていると、delete も add も
        # 展開後の別パスを対象にする。ルール名だけは一致するため rule_exists は
        # 真を返し、受信は通らないのに「完了」と表示される。cmd.exe の引数には
        # % を打ち消す手段が無いので、その場合は自動設定せずに理由を返す
        # （昇格済みの経路は netsh へ引数のリストで渡すので展開されない）。
        if "%" in prog:
            return False, ("実行ファイルのパスに % が含まれるため自exe受信許可を"
                           "自動設定できません（手動で追加してください）: " + prog)
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
        # 反映を確認できないものを成功にすると「通らないのに完了」と出る
        return False, "自exe受信許可を要求したが反映を確認できず: " + name
    except Exception as e:
        return False, "自exe受信許可の設定エラー: " + str(e)
