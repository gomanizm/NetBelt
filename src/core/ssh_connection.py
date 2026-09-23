"""SSH接続管理"""
import base64
import codecs
import hashlib
import os
import paramiko
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from PyQt6.QtCore import QObject, pyqtSignal

# known_hosts の読み書きを直列化する。同時に保存すると、あとから
# os.replace した側が先の結果を丸ごと差し替えてしまう。旧 known_hosts の
# 引き継ぎと接続前の読み込みも同じ錠を使うので、config_manager 側に置く。
# NetBelt を 2 つ起動した場合に備えて、プロセスをまたぐ錠も兼ねる
from .config_manager import known_hosts_guard as _known_hosts_guard


def known_hosts_server_name(host, port):
    """paramiko が known_hosts を引くときの名前を返す。

    既定ポートはホスト名そのまま、それ以外は "[host]:port"。
    SSHClient.connect が組み立てるのと同じ形にそろえる。
    """
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 22
    return host if port == 22 else "[%s]:%d" % (host, port)


def known_hosts_names(text):
    """known_hosts の 1 行が指す接続先名を返す（カンマ区切りは分解する）。

    OpenSSH の @cert-authority / @revoked は名前欄の前に置く印なので、
    印があるときは 1 つ読み飛ばす。paramiko 4.0.0 は印を剥がさずに
    読もうとして行ごと「読めない行」にしてしまうが、利用者から見れば
    正規の書式なので、どの接続先を指す行かはこちらで読み取る。
    """
    fields = text.split()
    if fields and fields[0].startswith("@"):
        fields = fields[1:]
    if not fields:
        return []
    return fields[0].split(",")


def _hostnames_match(names, server_name):
    """known_hosts の名前欄（カンマ区切り）が、その接続先を指すか"""
    for name in names:
        if name == server_name:
            return True
        # ハッシュ化された名前（|1|salt|hash）。paramiko の公開 API で
        # 同じ塩を使って掛け直し、一致するかを見る
        if name.startswith("|1|") and not server_name.startswith("|1|"):
            try:
                if paramiko.hostkeys.HostKeys.hash_host(server_name, name) == name:
                    return True
            except Exception:
                pass
    return False


def _iter_known_hosts_lines(path):
    """known_hosts を paramiko と同じ読み方で 1 行ずつ見る。

    返すのは (行番号, 行, 生バイト列, entry)。entry が None なら読めない行。

    paramiko 4.0.0 の HostKeyEntry.from_line は、フィールド不足や未知の
    鍵種別（sk-* や証明書）では None を返すが、鍵欄が base64 として
    壊れていると InvalidHostKey を投げる。これは SSHException を継承して
    いない素の Exception なので（実測: issubclass(...) → False）、
    HostKeys.load の except SSHException では拾えず、読み込み全体が例外に
    なる。@cert-authority / @revoked で始まる行も、印を剥がさないために
    3 つ目の欄が鍵種別の文字列になり、同じく InvalidHostKey になる。
    ここでは種類を問わず握りつぶし、読めない行として扱う。

    形の崩れたハッシュ化名（塩が 20 バイトに復号できない |1|AAAA|AAAA
    など）の行も、読めない行として扱う。from_line はそのまま通すが、
    paramiko の lookup はハッシュ化名の行ごとに hash_host を掛け直すので、
    接続先がどこであっても例外になる（実測: 接続のたびに本文の空な
    『接続エラー: 』だけが出て、全機器が繋がらなくなる）。
    """
    from paramiko.hostkeys import HostKeyEntry
    raw = Path(str(path)).read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        # Windows の編集ツール（メモ帳の「UTF-8 (BOM)」や PowerShell 5.1 の
        # Out-File -Encoding utf8）が付ける BOM。剥がさないと 1 行目の
        # ホスト名の頭に付いたまま登録され、その機器だけ黙って「未知」へ
        # 戻る（実測）。保存時の書き戻しと行の点検にも同時に効く
        raw = raw[len(codecs.BOM_UTF8):]
    for lineno, raw_line in enumerate(raw.split(b"\n"), 1):
        raw_line = raw_line.rstrip(b"\r")
        text = raw_line.decode("utf-8", errors="replace").strip()
        if not text or text.startswith("#"):
            continue
        try:
            entry = HostKeyEntry.from_line(text, lineno)
            if entry is not None:
                for name in entry.hostnames:
                    if name.startswith("|1|"):
                        paramiko.hostkeys.HostKeys.hash_host("x", name)
        except Exception:
            entry = None
        yield lineno, text, raw_line, entry


def unreadable_known_hosts_lines(path):
    """paramiko が読み込めない行を [(行番号, 行, 生バイト列)] で返す。

    壊れた行があっても paramiko の読み込みは成功したように見えたり
    （読み飛ばし）、逆に読み込み全体が例外になったりする。どちらでも
    その接続先は「未知のホスト」に戻って TOFU が何も聞かずに受け入れる
    か、関係のない機器まで繋がらなくなる。呼び出し側が行を名指しで
    知らせられるよう、paramiko と同じ読み方で拾う。

    生バイト列も返すのは、保存のときに書き戻して消さないため。
    """
    return [(lineno, text, raw)
            for lineno, text, raw, entry in _iter_known_hosts_lines(path)
            if entry is None]


def load_known_hosts(hostkeys, path):
    """読める行だけを paramiko の HostKeys へ入れる（例外を投げない）。

    paramiko の HostKeys.load / SSHClient.load_host_keys は、鍵欄が壊れた
    行や @cert-authority / @revoked の行があると例外になり、読める行まで
    失われる（実測: 1 行壊れているだけで全機器が繋がらなくなる）。
    読めない行は unreadable_known_hosts_lines() が名指しで知らせるので、
    ここでは読める行だけを取り込む。名前ごとに入れるのは
    HostKeys.load と同じ（複数名の行は名前の数だけ登録される）。

    行の入れ方も HostKeys.load にそろえる。HostKeys.add は
    (接続先, 鍵種別) が同じ既存エントリを置き換えるので、それを使うと
    同じ接続先・同じ鍵種別で食い違う 2 行のうち先の行が消える。
    OpenSSH は鍵の入れ替え期間に同じホストの行を並べることを許すので、
    利用者が新しい鍵の行を手で足した状態は普通に起きる。そこを潰すと、
    保存のたびにディスクを読み直す _save_known_hosts が、触っていない
    接続先の行を 1 本消してしまう（実測: A に食い違う 2 行がある状態で
    B の初回接続を保存すると、A は後の行だけになり、検証に使う鍵が
    入れ替わって次から BadHostKeyException で拒否される）。
    paramiko の検証（lookup → SubDict.__getitem__）は先頭の行を使うので、
    こちらも先に読んだ行を優先し、食い違う行は潰さずに並べる。
    同じ名前・同じ鍵種別・同じ鍵の行（同一行の重複）だけは畳む。

    畳むかどうかは名前を文字列のまま比べて決める。HostKeys.check は
    lookup 経由でハッシュ化名（|1|salt|hash）とも照合するので、それを
    使うと「ハッシュ行＋同じ鍵の平文行」の平文行が畳まれ、別の機器の
    保存で利用者の書いた行が黙って消える（実測）。
    """
    from paramiko.hostkeys import HostKeyEntry
    for _lineno, _text, _raw, entry in _iter_known_hosts_lines(path):
        if entry is None:
            continue
        keytype = entry.key.get_name()
        blob = entry.key.asbytes()
        for name in entry.hostnames:
            if any(name in e.hostnames and e.key.get_name() == keytype
                   and e.key.asbytes() == blob
                   for e in hostkeys._entries):
                continue
            # HostKeys.load 自身も _entries へ append する（paramiko 4.0.0）
            hostkeys._entries.append(HostKeyEntry([name], entry.key))


def _has_utf8_bom(path):
    """known_hosts の先頭に UTF-8 BOM があるか"""
    with open(str(path), "rb") as f:
        return f.read(len(codecs.BOM_UTF8)) == codecs.BOM_UTF8


def _load_known_hosts_into_client(client, path, broken):
    """known_hosts を client へ読み込む。

    読めない行が無ければ paramiko にそのまま読ませる（従来どおりの動き）。
    読めない行があるときだけ自前のローダを使う。paramiko に読ませると
    例外になり、読める行の鍵まで失って関係のない機器が繋がらなくなるため。

    先頭に UTF-8 BOM があるファイルも自前で読む。paramiko は BOM を
    剥がさないので、BOM を読めてしまう locale では 1 行目の名前が BOM
    付きで登録され、その機器だけ黙って「未知」に戻る（実測）。
    自前で読む経路では _host_keys_filename を None に戻す。この client は
    ファイルの一部しか持っていないので、paramiko の save_host_keys が
    この名前を見て書き出すことが二度と無いようにしておく（保存は
    _save_known_hosts がディスクから作り直して行う）。

    行としてはすべて読めるのに、ファイル全体が既定エンコーディングでは
    読めないことがある。paramiko の HostKeys.load は open(filename, "r")
    なので、この環境（cp932）では注釈欄やコメント行に日本語があるだけで
    UnicodeDecodeError になり、鍵の行が全部正しくても全機器が繋がらなく
    なる（実測）。行ごとの点検は bytes で読むので「読めない行」は 0 件で、
    行番号も出ない。デコードだけは受け止めて、読める行を取り込む。

    Args:
        broken: unreadable_known_hosts_lines() の戻り値
    """
    if broken or _has_utf8_bom(path):
        client._host_keys_filename = None
        load_known_hosts(client.get_host_keys(), path)
        return
    try:
        client.load_host_keys(str(path))
    except UnicodeDecodeError:
        # paramiko は読む前に _host_keys_filename を覚える。読めたのは
        # 一部だけなので、その名前を見て書き出されないよう戻す
        client._host_keys_filename = None
        load_known_hosts(client.get_host_keys(), path)


def _key_fingerprint(key):
    """鍵の指紋を OpenSSH と同じ形（SHA256:...）で返す"""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _refuse_conflicting_host_key(path, hostname, key):
    """ディスクに同じ接続先の別の鍵があれば、上書きせず中止する。

    missing_host_key が呼ばれるのは「読み込んだ時点でこの接続先の鍵を
    持っていなかった」ときだけなので、ここでディスクに鍵があるなら、
    自分が読んだあとに別の NetBelt（または別の接続）が保存したもの。
    そのまま保存すると、paramiko の書き出しが先に保存された鍵をすべて
    自分の鍵で置き換えてしまい（実測: 3 行とも後から来た鍵になる）、
    先に登録した機器は次から BadHostKeyException で繋がらなくなる。
    黙って上書きするより、食い違いを伝えて止める。

    Raises:
        HostKeyMismatchError: 同じ接続先に別の鍵が保存済みのとき
    """
    path = Path(str(path))
    if not path.exists():
        return
    disk = paramiko.HostKeys()
    # HostKeys.load は壊れた行で例外になる。ここで落ちると「保存できない」
    # 扱いになり、他の機器の初回鍵まで保存されなくなる
    load_known_hosts(disk, path)
    stored = disk.lookup(hostname)
    if stored is None or disk.check(hostname, key):
        return
    raise HostKeyMismatchError(
        "ホスト鍵が食い違います。%s の鍵として別の鍵が known_hosts に"
        "保存されているため、上書きせず接続を中止しました。\n"
        "  known_hosts の鍵: %s\n"
        "  今回提示された鍵: %s %s\n"
        "%s\n"
        "機器を入れ替えたなどで変更が意図したものなら、known_hosts の"
        "該当行を削除してから接続し直してください。"
        % (hostname,
           " / ".join("%s %s" % (name, _key_fingerprint(stored[name]))
                      for name in sorted(stored.keys())),
           key.get_name(), _key_fingerprint(key),
           path))


def _write_known_hosts_file(hostkeys, preserved, tmp_path):
    """書き出す内容を一時ファイルへ作る（本体の差し替えはしない）。

    HostKeys.save は _entries を 1 件 1 行で書き出すので、
    SSHClient.save_host_keys（items() を回し、lookup がその接続先の
    先頭エントリを返す）と違い、同じ行を何本も書いたり食い違う鍵を
    取り違えたりしない。
    """
    hostkeys.save(str(tmp_path))
    if preserved:
        # paramiko は text モードで書くので、改行もそれに合わせる
        with open(str(tmp_path), "ab") as f:
            for raw in preserved:
                f.write(raw + os.linesep.encode("ascii"))


def _save_known_hosts(known_hosts_path, entry):
    """今回の 1 件を known_hosts へ書き足す（ほかの行はディスクの現状のまま）。

    接続開始時に読み込んだ client の HostKeys を丸ごと書き戻していた頃は、
    その在庫が読み込んだ時点のものなので、あとでディスク側の行が消えても
    memory からは消えず、別の機器の保存が消された行を復活させていた
    （実測: 利用者が案内どおり該当行を削除して新しい鍵で登録し直すと、
    入れ直した鍵がファイルから消え、古い鍵が同じ行 3 本に増殖して唯一の
    正になる）。そこで、書く内容はそのつど錠の中でディスクから作り直し、
    今回保存する 1 件だけを足す。

    paramiko 4.0.0 の SSHClient.save_host_keys は保存先を "w" で開いて
    先に切り詰めるため、書いている途中で落ちると保存済みの鍵をまとめて
    失う。一時ファイルへ書いてから os.replace で差し替える。差し替えは
    不可分なので、途中で落ちても前の known_hosts がそのまま残る。

    Args:
        entry: (接続先名, 提示された鍵)。書き足す 1 件。書き込む前に、
            同じ接続先の別の鍵がディスクに無いかを錠の中で確かめる
    """
    path = Path(str(known_hosts_path))
    with _known_hosts_guard(path.parent):
        # 確かめてから書くまでを錠の中で通す。外で見ると、その間に
        # 別のプロセスが保存した鍵を見落とす
        _refuse_conflicting_host_key(path, entry[0], entry[1])
        hostkeys = paramiko.HostKeys()
        preserved = []
        if path.exists():
            # paramiko が読めない行は書き出しに入らないので、黙って消える。
            # 利用者が直すはずの行なので、そのまま書き戻す
            broken = unreadable_known_hosts_lines(path)
            preserved = [raw for _, _, raw in broken]
            # ほかの行は、いまディスクにあるものだけを引き継ぐ
            load_known_hosts(hostkeys, path)
        hostkeys.add(entry[0], entry[1].get_name(), entry[1])
        path.parent.mkdir(parents=True, exist_ok=True)
        # os.replace はドライブを跨げないので一時ファイルは同階層に作る
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        os.close(fd)
        try:
            _write_known_hosts_file(hostkeys, preserved, tmp_path)
            os.replace(tmp_path, str(path))
            tmp_path = None      # 差し替え済み。後片付けの対象から外す
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


class _TofuHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """未知ホストは受け入れて known_hosts に保存する(TOFU)。
    既知ホストで鍵が一致しない場合は paramiko が BadHostKeyException を送出する。
    """

    def __init__(self, known_hosts_path):
        self._known_hosts_path = known_hosts_path

    def missing_host_key(self, client, hostname, key):
        client.get_host_keys().add(hostname, key.get_name(), key)
        try:
            _save_known_hosts(self._known_hosts_path, (hostname, key))
        except HostKeyMismatchError:
            # 食い違いは「保存できなかった」ではなく「保存してはいけない」。
            # 警告で済ませず、そのまま接続を中止させる（client はこのあと
            # 接続処理の後始末で閉じられる）
            raise
        except Exception as e:
            # 黙って続けると、次回もこの機器の鍵を検証できないまま任意の
            # 鍵を受け入れる。接続は続けるが、そのことを画面に出す
            on_save_error = getattr(self, "_on_save_error", None)
            if on_save_error is not None:
                on_save_error(
                    "known_hosts を保存できません（%s）。次回、この機器の鍵を"
                    "検証できません: %s" % (e, self._known_hosts_path))


class HostKeyStoreError(Exception):
    """既知ホスト鍵の保存場所を読めない。検証できない状態で認証へ進まない"""


class HostKeyMismatchError(Exception):
    """同じ接続先の別の鍵が known_hosts にある。上書きせず接続を中止する"""


class SSHConnection(QObject):
    """SSH接続を管理するクラス"""
    
    # シグナル定義
    output_received = pyqtSignal(str)  # 出力を受信
    connected = pyqtSignal()  # 接続成功
    disconnected = pyqtSignal()  # 切断
    error_occurred = pyqtSignal(str)  # エラー発生
    
    def __init__(self, host: str, port: int, username: str, password: str = "", 
                 ssh_key: str = "", parent=None):
        """
        初期化
        
        Args:
            host: ホスト名またはIPアドレス
            port: ポート番号
            username: ユーザー名
            password: パスワード
            ssh_key: SSH秘密鍵ファイルのパス
            parent: 親オブジェクト
        """
        super().__init__(parent)
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.ssh_key = ssh_key
        
        self.client: Optional[paramiko.SSHClient] = None
        self.channel: Optional[paramiko.Channel] = None
        self.is_connected = False
        # 端末の大きさ。接続前に set_terminal_size で上書きされる
        self.term_cols = 80
        self.term_rows = 24
        self._read_thread: Optional[threading.Thread] = None
        # 読み取りの停止と、接続中に後始末が走ったことを兼ねる印。
        # connect() の入口で戻し、接続が成立したあとにもう一度見る
        self._stop_reading = False
        # dispose() 済み（このオブジェクトは捨てられた）ことを覚えておく印。
        # _stop_reading と違い connect() の入口で戻さないので、接続スレッドが
        # 動き出す前に着地した dispose() でも消えない
        self._disposed = False
        # 画面の描き待ちが多すぎる間、受信を止めておくための関所
        # （TerminalWidget.output_gate。set_read_gate で受け取る）
        self._read_gate = None

    def set_read_gate(self, gate) -> None:
        """受信を止める合図（threading.Event）を受け取る

        set されている間だけチャネルから読む。閉じている間は recv を
        呼ばないので paramiko がチャネルの窓を広げず、機器側は送るのを
        待つ。捨てずに待たせるので、記録には全量が残る。
        """
        self._read_gate = gate

    def _wait_while_gated(self) -> bool:
        """受信を止められていれば少し待つ。待ったなら True

        待ちは短く区切る。止められている間も、停止（_stop_reading）や
        切断に気づけるようにするため。
        """
        gate = self._read_gate
        if gate is None or gate.is_set():
            return False
        gate.wait(0.05)
        return True


    def _setup_host_keys(self, client):
        """既知ホスト鍵を読み込み、TOFUポリシーを設定する。
        既知ホストで鍵が変わった場合は接続時に BadHostKeyException となる。
        """
        from . import config_manager
        known_hosts_path = config_manager.app_data_dir() / "known_hosts"
        # 旧 ~/.terminal-tool からの引き継ぎに失敗していたら、それを伏せない。
        # 既知のはずの機器が「未知」に戻り、確認なしで受け入れられる
        import_warning = config_manager.take_known_hosts_import_warning()
        if import_warning:
            self.output_received.emit(
                "\r\n[NetBelt] 警告: %s\r\n" % import_warning)
        # 他の接続の保存や引き継ぎが差し替えている最中に読まない。Windows では
        # Permission denied になり、下の中止に落ちる。NetBelt を 2 つ起動して
        # いると別プロセスの保存ともぶつかるので、錠はプロセスをまたぐ
        broken = []
        try:
            with _known_hosts_guard(known_hosts_path.parent):
                if known_hosts_path.exists():
                    # 読めない行の点検が先。paramiko の読み込みは壊れた行が
                    # あると例外になるので、あとに回すと点検まで辿り着けない
                    broken = unreadable_known_hosts_lines(known_hosts_path)
                    # paramiko は読めない行を黙って読み飛ばす。放っておくと
                    # その接続先は「未知のホスト」に戻り、TOFU が何も聞かずに
                    # 提示された鍵を受け入れる（鍵が変わっていても分からない）
                    _load_known_hosts_into_client(
                        client, known_hosts_path, broken)
        except Exception as e:
            # 握りつぶして TOFU にすると、既知の機器でも「未知」扱いになり、
            # 鍵が変わっていても気づかずにパスワードを送る。検証できない
            # 状態で認証へ進まない
            raise HostKeyStoreError(
                "既知ホスト鍵 (known_hosts) を読めないため接続を中止しました: %s\n%s\n"
                "ファイルを開けない（権限・排他・入出力エラー）か、中身を"
                "読み取れない状態です。権限を修正するか、該当行を修正・削除"
                "するか、ファイルを退避してから接続し直してください"
                "（退避すると全機器が初回接続の扱いになります）。"
                % (e, known_hosts_path))
        if broken:
            self._refuse_or_warn_broken_lines(broken, known_hosts_path)
        policy = _TofuHostKeyPolicy(known_hosts_path)
        policy._on_save_error = lambda message: self.output_received.emit(
            "\r\n[NetBelt] 警告: %s\r\n" % message)
        client.set_missing_host_key_policy(policy)

    def _refuse_or_warn_broken_lines(self, broken, known_hosts_path):
        """読めない行を名指しで知らせ、その行が指す接続先なら接続を中止する。

        壊れた行を読み飛ばしたまま進むと、その接続先は初回接続の扱いに戻り、
        鍵が変わっていても TOFU が黙って受け入れてしまう。そこで、いま繋ご
        うとしている接続先を指す行があるときだけ中止する。名前欄が完全に
        一致しなかった行は、知らせるだけで接続は今までどおり続ける
        （名前欄のワイルドカードは照合しないので、そちらは警告で伝える）。

        Args:
            broken: unreadable_known_hosts_lines() の戻り値
            known_hosts_path: known_hosts のパス（案内に載せる）
        """
        server_name = known_hosts_server_name(self.host, self.port)
        mine = [(no, text) for no, text, _ in broken
                if _hostnames_match(known_hosts_names(text), server_name)]
        if mine:
            raise HostKeyStoreError(
                "known_hosts に読めない行があり、%s の鍵を検証できないため"
                "接続を中止しました:\n%s\n%s\n"
                "この行を直すか削除してから接続し直してください"
                "（削除するとこの機器は初回接続の扱いになります）。"
                % (server_name,
                   "\n".join("  %d 行目: %s" % (no, text) for no, text in mine),
                   known_hosts_path))
        # ここに来るのは「名前欄が完全一致しなかった」だけ。名前欄の
        # ワイルドカード（* や ?）は照合していないので、この機器を指す行で
        # ないとは言い切れない。実測: `@cert-authority *.example.com` の 1 行
        # だけを置いて sw1.example.com へ繋ぐと、この警告を出したうえで
        # TOFU が何も聞かずに鍵を受け入れる。SSH CA を使う組織の
        # known_hosts はこの形の行をそのまま持っているので、断定すると
        # 逆向きに安心させることになる
        self.output_received.emit(
            "\r\n[NetBelt] 警告: known_hosts に読めない行があります"
            "（名前欄がこの機器の接続先と完全には一致しないので、"
            "接続は続けます）。ただしワイルドカード（* や ?）を含む行は"
            "この機器を指している可能性があり、その場合この機器は初回接続の"
            "扱いに戻り、鍵が変わっていても気づけません:\r\n"
            "%s\r\n%s\r\n"
            % ("\r\n".join("  %d 行目: %s" % (no, text)
                           for no, text, _ in broken),
               known_hosts_path))

    def _auth_failure_message(self) -> str:
        """認証失敗の理由を、実際に使った手段に合わせて返す。

        すべてを「ユーザー名またはパスワードが間違っています」と報告すると、
        鍵で認証しているときに、存在しないパスワードを疑わせることになる。
        ユーザー名が空のときは機器側ではなく設定の問題なので、そう名指しする。
        """
        if not self.username:
            return ("認証失敗: ユーザー名が設定されていません。"
                    "デバイスの設定でユーザー名を入力してください。")
        if self.ssh_key:
            # 鍵を指定したときはパスワードを一切使わない。入力されて
            # いると「パスワードも試された」と誤解されるので、そう書く。
            note = ("なお、鍵を指定しているためパスワードは使っていません。"
                    if self.password else "")
            return ("認証失敗: 指定した鍵がユーザー %s では受け付けられません"
                    "でした。機器側の authorized_keys にこの鍵の公開鍵が"
                    "登録されているか、ユーザー名が合っているかを"
                    "確認してください。%s" % (self.username, note))
        return "認証失敗: ユーザー名またはパスワードが間違っています"

    # シェルが開くのを待つ上限。
    # paramiko の channel_timeout（既定 3600 秒）が効くのは CHANNEL_OPEN
    # までで、その後の pty-req / shell 要求は channel.py の
    # _wait_for_event() が引数なしの event.wait() で待つため無期限になる。
    # connect_kwargs へ channel_timeout を足してもこの段階は救えない。
    # 認証が通ったあとなので、機器側のログイン猶予も効かない。
    SHELL_TIMEOUT_SECONDS = 30

    def _open_shell(self, client):
        """インタラクティブシェルを開く（上限まで待って開かなければ None）

        応答を返さない機器に当たると invoke_shell が無期限に止まり、
        connected も error_occurred も出ないまま接続スレッドが居座る。
        UI は「接続します...」と空のタブのままで、失敗表示も再接続の
        案内も出ないので、利用者からは固まったようにしか見えない。

        別スレッドで開かせ、上限を過ぎたら諦める。取り残されたスレッドは
        呼び出し側（_fail -> dispose）が接続を閉じた時点で例外になって
        終わる。daemon なのでアプリの終了も妨げない。

        client は connect() が握っているローカル参照を受け取る。self.client を
        見にいくと、待っている間に dispose() が走った場合に None になっている。
        """
        outcome = {}

        def open_it():
            try:
                outcome['channel'] = client.invoke_shell(
                    term='vt100', width=self.term_cols, height=self.term_rows)
            except Exception as e:
                outcome['error'] = e

        worker = threading.Thread(target=open_it, daemon=True)
        worker.start()
        worker.join(timeout=self.SHELL_TIMEOUT_SECONDS)

        if worker.is_alive():
            return None
        if 'error' in outcome:
            # 例外はこれまでどおり呼び出し側の except で分類させる
            raise outcome['error']
        return outcome.get('channel')

    @staticmethod
    def _close_transports(transports):
        """client.connect() の中で作られた Transport を直接閉じる。

        dispose() が Transport の作成から start_client までの間に着地すると、
        client.close() は「まだ動いていない」Transport を閉じずに（paramiko の
        Transport.close() は active でなければ何もしない）参照だけ捨てる。
        そのあと動き出した Transport には client からたどれないので、作った
        ときに覚えておいたものを閉じる。閉じ済みのものには何もしない。
        """
        for transport in transports:
            try:
                transport.close()
            except Exception:
                pass

    def _fail(self, message: str, client=None, transports=()) -> bool:
        """接続に失敗したときの後始末と通知

        paramiko の SSHClient.connect() は失敗しても自分ではトランスポートを
        閉じない。閉じずに戻ると、機器へ張った TCP セッションと Transport
        スレッドが生き残る。呼び出し側も失敗時は disconnect() を呼ばないので、
        SSHConnection を捨てても Transport スレッド自身がオブジェクトを
        参照し続け、GC でも回収されない。

        機器側は認証前のログイン猶予（Cisco IOS の ip ssh time-out、
        OpenSSH の LoginGraceTime、いずれも既定 120 秒）でいずれ切るが、
        invoke_shell の失敗は認証が通ったあとなので猶予が効かない。

        client は connect() が握っているローカル参照。接続を待っている間に
        dispose() が先に走ると self.client は None になっており、dispose()
        ではそのあと作られた Transport を閉じられない。その場合はここで閉じる。
        transports は connect() が覚えている Transport（_close_transports 参照）。
        """
        orphan = client if client is not None and client is not self.client else None
        self.dispose()
        if orphan is not None:
            try:
                orphan.close()
            except Exception:
                pass
        self._close_transports(transports)
        self.error_occurred.emit(message)
        return False

    def _abandon(self, client, channel, transports=()) -> bool:
        """破棄済みの接続で成立してしまった分を閉じ、失敗として戻る。

        利用者がタブを閉じただけなので error_occurred は出さない。ここで
        内部例外の文面を出すと、閉じた覚えのない「接続エラー」に見える。
        """
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass
        try:
            client.close()
        except Exception:
            pass
        self._close_transports(transports)
        return False

    def connect(self) -> bool:
        """
        SSH接続を開始

        Returns:
            bool: 接続成功時True
        """
        client = None       # 例外の後始末で閉じるため、try の外で用意する
        # client.connect() の中で作られた Transport。後始末で直接閉じる
        transports = []

        def transport_factory(*args, **kwargs):
            transport = paramiko.Transport(*args, **kwargs)
            transports.append(transport)
            return transport

        try:
            if self._disposed:
                # 接続スレッドが動き出す前にタブが閉じられ、dispose() が先に
                # 走った。ここで印を無視して進むと、接続は最後まで成立する
                # のに参照しているものが誰もいない状態になり、閉じる経路が
                # 無いまま機器の vty 枠を掴んだままになる
                return False

            # 待っている間に dispose() が走ると self.client は None になる。
            # 後始末は必ずこのローカル参照に対して行う
            client = paramiko.SSHClient()
            self.client = client
            self._stop_reading = False
            try:
                self._setup_host_keys(client)
            except HostKeyStoreError as e:
                return self._fail(str(e))
            
            # 接続パラメータの準備
            connect_kwargs = {
                'hostname': self.host,
                'port': self.port,
                'username': self.username,
                'timeout': 20,
                'look_for_keys': False,  # ローカルキーを探さない
                'allow_agent': False,     # SSHエージェントを使わない
                'banner_timeout': 30,     # バナー待機時間を増やす
                'auth_timeout': 30,       # 認証タイムアウトを増やす
                # 作った Transport を覚える（_close_transports を参照）
                'transport_factory': transport_factory,
            }
            
            # パスワードまたは秘密鍵で認証
            if self.ssh_key:
                try:
                    # 鍵タイプを自動判別。paramiko 4.0.0 で DSA(DSSKey) は
                    # 削除されているので並べない。存在しない属性を並べると
                    # リストを組む時点で AttributeError になり、正常な鍵でも
                    # 「読み込みエラー」で接続できなくなる。
                    key = None
                    key_errors = []
                    needs_passphrase = False
                    for key_class in (paramiko.RSAKey, paramiko.Ed25519Key,
                                      paramiko.ECDSAKey):
                        try:
                            key = key_class.from_private_key_file(self.ssh_key)
                            break
                        except paramiko.PasswordRequiredException as e:
                            # 例外の文言に password の語が無いので型で覚えておく
                            needs_passphrase = True
                            key_errors.append(f"{key_class.__name__}: {e}")
                        except Exception as e:
                            key_errors.append(f"{key_class.__name__}: {e}")

                    if key is None:
                        # 集めた理由を捨てない。特にパスフレーズ付きの鍵は
                        # 「対応する鍵タイプが無い」と出ると原因が分からない。
                        if needs_passphrase:
                            return self._fail(
                                "秘密鍵の読み込みエラー: この鍵はパスフレーズで保護されています。"
                                "パスフレーズ無しの鍵を指定してください。")
                        return self._fail(
                            "秘密鍵の読み込みエラー: 対応する鍵タイプが見つかりません。\n"
                            + "\n".join(key_errors))

                    connect_kwargs['pkey'] = key
                    # 指定された鍵だけを使う。True にすると、その鍵が拒否された
                    # ときに ~/.ssh の別の鍵で認証が通ってしまい、利用者が
                    # 意図したのと違う身元で接続することになる。
                    connect_kwargs['look_for_keys'] = False
                except Exception as e:
                    return self._fail(f"秘密鍵の読み込みエラー: {str(e)}")
            elif self.password:
                connect_kwargs['password'] = self.password
            else:
                return self._fail("パスワードまたは秘密鍵が必要です")
            
            # SSH接続を実行
            client.connect(**connect_kwargs)

            if self._stop_reading:
                # 名前解決や TCP 接続を待っている間にタブが閉じられた。
                # dispose() が呼んだ close() は Transport の登録前や動き出す前で
                # 何もしていないので、ここで閉じないと成立したセッションとスレッドが
                # 残り、機器の vty 枠を掴んだままになる
                return self._abandon(client, None, transports)
            
            # インタラクティブシェルを開始 (RFC 4254 6.2 pty-req)
            channel = self._open_shell(client)
            if channel is None:
                return self._fail(
                    "シェルを開けませんでした（%d 秒待って応答がありません）。\n"
                    "機器が混んでいる、exec 認可の応答を待っている、"
                    "vty が空いていない、などが考えられます。"
                    % self.SHELL_TIMEOUT_SECONDS)
            channel.settimeout(0.1)
            
            if self._stop_reading:
                # シェルを開いている間に閉じられた場合も同じ
                return self._abandon(client, channel, transports)

            self.channel = channel
            self.is_connected = True
            self.connected.emit()
            
            # 読み取りスレッドを開始
            self._read_thread = threading.Thread(target=self._read_output, daemon=True)
            self._read_thread.start()
            
            return True
            
        except HostKeyMismatchError as e:
            # 文言は保存側が組み立てている（どちらの鍵かを含む）ので、
            # そのまま出す
            return self._fail(str(e), client, transports)
        except paramiko.AuthenticationException:
            return self._fail(self._auth_failure_message(), client, transports)
        except paramiko.BadHostKeyException:
            return self._fail(
                "ホストキーが変更されています(中間者攻撃の可能性)。"
                "意図的な変更の場合は ~/.netbelt/known_hosts の該当ホスト行を削除してください。",
                client, transports)
        except paramiko.SSHException as e:
            return self._fail(f"SSH接続エラー: {str(e)}", client, transports)
        except Exception as e:
            return self._fail(f"接続エラー: {str(e)}", client, transports)
    
    def dispose(self):
        """チャネルと SSHClient を閉じて資源を手放す（通知は出さない）

        機器側都合の切断やエラーを受けたあとの後始末で使う。ここで
        disconnected を出すと、いま処理中の切断処理が再入する。
        閉じずに参照だけ捨てると、Transport スレッド自身がオブジェクトを
        参照し続けるため GC でも回収されない。

        これを呼んだあとの connect() は、何もせず False を返す。呼び出し元
        （MainWindow._close_connection / _discard_stale）は dispose() の前に
        接続辞書からこのオブジェクトを外しており、以後この接続を使う人は
        いないため。同じオブジェクトで繋ぎ直す場合は disconnect() を使う。

        残る制限: 接続スレッドが動き出す前の disconnect() は、繋ぎ直しの
        ために印を消すので取り消しにならない。利用者が明示的に切断してから
        タブを残す経路（MainWindow._on_disconnect_device）だけなので、この
        場合は接続が成立しても同じオブジェクトが保持し続ける。
        """
        self._disposed = True
        self._stop_reading = True
        self.is_connected = False

        if self._read_thread and self._read_thread.is_alive():
            self._read_thread.join(timeout=2)

        if self.channel:
            self.channel.close()
            self.channel = None

        if self.client:
            self.client.close()
            self.client = None

    def disconnect(self):
        """SSH接続を切断（同じオブジェクトで繋ぎ直せる）"""
        self.dispose()
        # dispose() の印は「このオブジェクトは捨てた」意味なので、利用者が
        # 明示的に切断しただけの場合は消す。残すと次の connect() が
        # 取り消し扱いになり、繋ぎ直せなくなる
        self._disposed = False
        self.disconnected.emit()
    
    def send_command(self, command: str):
        """
        コマンドを送信（キー入力をそのまま送信）
        
        Args:
            command: 送信するコマンド（1文字または制御文字）
        """
        if not self.is_connected or not self.channel:
            return
        
        try:
            # キー入力をそのまま送信（改行は追加しない）
            # InteractiveTerminalからEnterキーは'\r'として送られてくる
            # send は送れたバイト数を返すだけで、渡した全部を送ったとは
            # 限らない。1文字ずつ送っていた頃はまず起きなかったが、
            # 貼り付けをまとめて渡すようになったので取りこぼしうる。
            self.channel.sendall(command.encode('utf-8'))
        except Exception as e:
            self.error_occurred.emit(f"送信エラー: {str(e)}")

    def set_terminal_size(self, cols: int, rows: int):
        """端末の大きさを機器へ伝える (RFC 4254 6.7 window-change)。

        接続前に呼ばれたら、接続時の pty 要求 (6.2) に使う。
        通知に失敗しても接続はそのまま続ける。
        """
        self.term_cols = cols
        self.term_rows = rows
        if not self.is_connected or not self.channel:
            return
        try:
            self.channel.resize_pty(width=cols, height=rows)
        except Exception:
            pass

    def _read_output(self):
        """バックグラウンドで出力を読み取る"""
        # 受信の切れ目で割れた多バイト文字を、次の受信と繋いで復号する。
        # 受信ごとに復号すると、前半と後半がそれぞれ U+FFFD になり、
        # 画面にもセッションログにも化けたまま渡る
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        while not self._stop_reading and self.is_connected:
            try:
                if self._wait_while_gated():
                    continue
                if self.channel and self.channel.recv_ready():
                    data = self.channel.recv(4096)
                    if data:
                        text = decoder.decode(data)
                        # リアルタイムで出力（バッファリングなし）
                        if text:
                            self.output_received.emit(text)
                    else:
                        # データがないのにrecv_readyがTrueの場合は接続が閉じられた
                        if self.is_connected:
                            self.is_connected = False
                            self.disconnected.emit()
                        break
                else:
                    # チャネルが閉じられているかチェック
                    if self.channel and self.channel.closed:
                        if self.is_connected:
                            self.is_connected = False
                            self.disconnected.emit()
                        break
                    time.sleep(0.01)
                    
            except Exception as e:
                if self.is_connected:
                    self.is_connected = False
                    self.disconnected.emit()
                break
        # 切れ目で終わった未完の文字を捨てない
        rest = decoder.decode(b'', final=True)
        if rest:
            self.output_received.emit(rest)
