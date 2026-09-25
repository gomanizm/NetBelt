"""設定ファイル管理モジュール"""
import contextlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set
from .crypto import PasswordCrypto

# ファイルロックの実装。依存パッケージは足さず、標準ライブラリだけで作る
try:
    import msvcrt          # Windows
except ImportError:        # pragma: no cover - 本アプリは Windows 専用
    msvcrt = None
try:
    import fcntl           # POSIX（開発時の参考実装）
except ImportError:        # pragma: no cover - Windows には無い
    fcntl = None

# 機器名として使えない名前。ターミナルはホームタブをタブ名 "ホーム" で
# 見分けているので、同名の機器はタブを閉じられず、ログ保存・記録・
# マクロ設定も「ホームタブ」扱いで断られる。
RESERVED_DEVICE_NAMES = ("ホーム",)


def is_reserved_device_name(name) -> bool:
    """その名前が機器名として予約されているか（前後の空白は無視）"""
    return isinstance(name, str) and name.strip() in RESERVED_DEVICE_NAMES


def is_readable_macro(macro) -> bool:
    """そのマクロを画面に出して実行できるか

    条件は「辞書で、名前が文字列で、commands が文字列だけの list」。
    読み込み時の隔離（_quarantine_invalid_devices）と、3 つの読み手
    （DeviceTree の「ツール」・MacroDialog のプリセット一覧・DeviceDialog の
    マクロ一覧）が同じ条件を使うためにここへ置く。以前は 4 か所がそれぞれ
    「辞書かどうか」だけを見ていたため、辞書ではあるが name が文字列でない
    要素（手編集の config の {"name": 5, ...} など）はすべて素通りし、
    名前を画面へ入れる addAction / addItem が TypeError で落ちて、
    右クリック・マクロ設定・機器編集が開かなくなっていた。

    commands も同じ理由で見る。送信側は commands[index] で 1 要素ずつ
    取り出して CR を付けて送るので、"show version" のような文字列は
    1 文字ずつのコマンドに化けて実機へ流れ、数値・辞書・非文字列を含む
    list は送信を仕掛ける QTimer のスロットの中で TypeError / KeyError に
    なる。PyQt6 はスロット内の未捕捉例外で終了するため、1 行送った後に
    アプリごと落ちる。グループの auto_commands は _is_valid_auto_commands が
    同じ条件で弾いており、マクロにだけこの検査が無かった。
    commands キーが無いのは「コマンド未設定」なので、既定値 [] で通す。
    """
    return (isinstance(macro, dict)
            and isinstance(macro.get("name"), str)
            and isinstance(macro.get("commands", []), list)
            and all(isinstance(c, str) for c in macro.get("commands", [])))


def count_macros_named(macros, name) -> int:
    """共通マクロの一覧で、その名前を持つものの数（一覧が list でなければ 0）

    マクロの実行・編集・削除は名前で相手を引く（get_macro_by_name は先頭の
    1 件、remove_global_macro は同じ名前の全件）。手編集や持ち込みの
    config.json で同じ名前が並ぶと、2 件目を選んでも 1 件目のコマンドが
    機器へ送られ、削除では両方が消えていた（実測）。名前で引く操作は、
    これが 2 以上なら断る。
    """
    if not isinstance(macros, list):
        return 0
    return sum(1 for m in macros
               if isinstance(m, dict) and m.get("name") == name)


def device_endpoint(device_data):
    """機器データが指す接続先を、比べられる形で返す（辞書でなければ None）

    自動検出か登録か・プロトコル・ホストとポート（シリアルはポート名）。
    接続処理が実際に使う値と同じ読み方をする。機器名が同じでも接続先が
    違えば別の機器なので、名前だけで 1 件に絞れないときの決め手に使う。
    画面側（MainWindow._endpoint_of）もこの関数を呼ぶ。
    """
    if not isinstance(device_data, dict):
        return None
    autodetect = device_data.get('source') == 'autodetect'
    protocol = device_data.get('protocol', 'ssh')
    if protocol in ('serial', 'console'):
        if protocol == 'console':
            return (autodetect, 'serial', device_data.get('host', ''))
        return (autodetect, 'serial', str(device_data.get('port', '')))
    default_port = 23 if protocol == 'telnet' else 22
    return (autodetect, 'telnet' if protocol == 'telnet' else 'ssh',
            device_data.get('host', 'unknown'),
            str(device_data.get('port', default_port)))


# 旧 ~/.terminal-tool/known_hosts を引き継げなかったときの警告文。
# app_data_dir() が呼ばれるたびに更新する。
_known_hosts_import_warning = None

# 引き継ぎ済みの目印。これができるまで毎回やり直す
_IMPORT_MARKER_NAME = "known_hosts.imported"

# known_hosts の読み書きを直列化する。旧 known_hosts の引き継ぎ・ホスト鍵の
# 保存（ssh_connection._save_known_hosts）・接続前の読み込みが共有する。
# 引き継ぎが読んだあとに別の接続が鍵を保存すると、引き継ぎの os.replace が
# 古い内容で差し替えてその鍵を消す。Windows では差し替えの最中に読むと
# Permission denied になり、接続が中止される
known_hosts_lock = threading.Lock()

# 別プロセス（NetBelt を 2 つ起動した状態）との排他に使うロック用ファイル。
# known_hosts 本体は os.replace で差し替わるので、鍵をかける相手にできない
_KNOWN_HOSTS_LOCK_NAME = "known_hosts.lock"

# ロックを待つ上限。取れないまま待たせ続けない
KNOWN_HOSTS_LOCK_TIMEOUT = 5.0


class KnownHostsLockError(Exception):
    """ホスト鍵ファイルの順番待ちが上限に達した"""


def _try_known_hosts_lock(fd) -> bool:
    """ロックを 1 回だけ試す（待たない）。取れたら True"""
    try:
        if msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        elif fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release_known_hosts_lock(fd) -> None:
    try:
        if msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def _known_hosts_file_lock(directory, timeout=None):
    """known_hosts の読み書きを、別プロセスとも直列化する。

    known_hosts_lock は threading.Lock なので同じプロセスの中しか守らない。
    NetBelt を 2 つ起動すると、片方の os.replace ともう片方の読み書きが
    Windows でぶつかる。実測では保存 300 回のうち 229 回が
    PermissionError [WinError 5] になって鍵が残らず、読み込み 3000 回の
    うち 6 回が Permission denied で接続の中止に落ちた。

    ロック用ファイルすら開けないときは、プロセス内の錠だけで今までどおり
    続ける（ここで接続や保存を止める方が影響が大きい）。取り合いで上限を
    過ぎたときだけ KnownHostsLockError を投げ、呼び出し側の既存の警告・
    中止の文言に載せる。
    """
    if timeout is None:
        timeout = KNOWN_HOSTS_LOCK_TIMEOUT
    lock_path = Path(str(directory)) / _KNOWN_HOSTS_LOCK_NAME
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        yield False
        return
    try:
        deadline = time.monotonic() + timeout
        while not _try_known_hosts_lock(fd):
            if time.monotonic() >= deadline:
                raise KnownHostsLockError(
                    "ほかの NetBelt がホスト鍵ファイルを使っているため、"
                    "%.0f 秒待っても順番が来ませんでした: %s"
                    % (timeout, lock_path.parent / "known_hosts"))
            time.sleep(0.01)
        try:
            yield True
        finally:
            _release_known_hosts_lock(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def known_hosts_guard(directory, timeout=None):
    """known_hosts を触る間、同じプロセスの中とも別プロセスとも排他する。

    ファイルロックは必ずこの順（threading.Lock が外側）で取る。逆順や
    入れ子にすると、同じプロセスの別スレッドが別のハンドルで同じ範囲を
    掴みにいき、自分自身を待つことになる。
    """
    with known_hosts_lock:
        with _known_hosts_file_lock(directory, timeout) as cross_process:
            yield cross_process


def take_known_hosts_import_warning():
    """旧 known_hosts を引き継げなかったときの警告文を取り出す（無ければ None）。

    一度返したら消す。接続のたびに同じ文言を出し続けないため。
    """
    global _known_hosts_import_warning
    warning = _known_hosts_import_warning
    _known_hosts_import_warning = None
    return warning


def _known_hosts_entry_id(line):
    """known_hosts の 1 行を (印, ホスト, 鍵種別) で見分ける。

    引き継ぎで「新しい側に既にある行」を見分けるためだけに使う。
    見分けようのない行（欄が 3 つに満たない行・コメント行）は None を返す。

    OpenSSH の @cert-authority / @revoked は名前欄の前に置く印なので、
    印があるときは 1 つ読み飛ばしてからホストと鍵種別を見る。剥がさないと
    見分けが (印, ホスト) の 2 つになり、同じホストの @revoked 行が鍵種別
    ごとに 2 行あっても 1 行しか引き継がれない（実測）。印そのものも
    見分けに含める。印の有無で意味が変わる行なので、同じ行にはできない。
    """
    fields = line.split()
    if fields and fields[0].startswith("#"):
        # コメント。中身は鍵ではないので、行の同一性を鍵の欄では測れない
        return None
    marker = ""
    if fields and fields[0].startswith("@"):
        marker = fields[0]
        fields = fields[1:]
    if len(fields) < 3:
        return None
    return (marker, fields[0], fields[1])


def _import_legacy_known_hosts(new_dir):
    """引き継ぎを、別プロセスの保存・読み込みとも排他して行う。

    引き継ぎは known_hosts を読んで os.replace で差し替えるので、ほかの
    NetBelt の保存・読み込みとぶつかる。目印ができるまでの一度きりなので、
    先に安い確認で振るい、その 1 回だけロックを取る（毎回のロック取得を
    接続の経路に足さない）。
    """
    old_kh = Path.home() / ".terminal-tool" / "known_hosts"
    marker = new_dir / _IMPORT_MARKER_NAME
    if marker.exists() or not old_kh.exists():
        return None
    try:
        with _known_hosts_file_lock(new_dir):
            if marker.exists():
                # 待っている間に、別プロセスが引き継ぎを終えていた
                return None
            return _import_legacy_known_hosts_unlocked(new_dir)
    except Exception as e:
        return ("旧 %s を引き継げませんでした（%s）。引き継げるまで、"
                "この機器は初回接続として扱われ、鍵が変わっていても"
                "気づけません。手でコピーしてください: %s"
                % (old_kh, e, new_dir / "known_hosts"))


def _import_legacy_known_hosts_unlocked(new_dir):
    """旧 ~/.terminal-tool/known_hosts の行を引き継ぐ。

    引き継げなかったときは警告文を返す（握り潰さない）。黙って続けると、
    既知の機器が「未知」に戻り、TOFU ポリシーが何も聞かずに新しい鍵を
    受け入れる。既知ホスト鍵を読めないなら接続を中止する、という方針の
    抜け道になる。

    引き継ぎ済みかどうかは目印ファイルで見る。「新しい known_hosts が
    あるか」で見ると、TOFU が先にファイルを作った時点で二度とやり直され
    なくなり、旧い鍵が恒久的に捨てられる。
    """
    old_kh = Path.home() / ".terminal-tool" / "known_hosts"
    marker = new_dir / _IMPORT_MARKER_NAME
    if marker.exists() or not old_kh.exists():
        return None
    new_kh = new_dir / "known_hosts"
    try:
        # utf-8-sig で読む。メモ帳や PowerShell 5.1 が付けた先頭の BOM を
        # 剥がすだけで、ほかは utf-8 と同じ。剥がさずに読むと 1 行目の
        # ホスト名が BOM 付きになり、新しい known_hosts の「途中」へ
        # そのまま書かれる。途中の BOM は読み込み側では剥がせないので、
        # その機器だけ黙って「未知」へ戻り（TOFU が別の鍵を受け入れる）、
        # cp932 環境では以後の保存が毎回失敗して新しい鍵が残らない。
        old_lines = old_kh.read_text(
            encoding="utf-8-sig", errors="replace").splitlines()
        current = (new_kh.read_text(encoding="utf-8-sig", errors="replace")
                   .splitlines() if new_kh.exists() else [])
        known = set(filter(None, (_known_hosts_entry_id(l) for l in current)))
        added = []
        for line in old_lines:
            if not line.strip():
                continue       # 空行は情報を持たないので持ち越さない
            entry_id = _known_hosts_entry_id(line)
            if entry_id is None:
                # 欄が 3 つに満たない行（鍵欄が書き込み途中で切れた等）と
                # コメント行。捨てると「このファイルは壊れている」合図まで
                # 消え、旧ファイルを見ていれば断っていた接続先が、引き継ぎを
                # 境に黙って初回接続（TOFU）へ戻る（実測）。重複の見分けは
                # できないので、判定の対象にはせず、そのまま書き出す
                added.append(line)
            elif entry_id not in known:
                added.append(line)
        if added:
            # 本体を直接開くと、その瞬間に切り詰められる。同階層へ書いて
            # から os.replace で差し替える（差し替えは不可分）。
            fd, tmp_path = tempfile.mkstemp(
                dir=str(new_dir), prefix=new_kh.name + ".", suffix=".tmp")
            try:
                with os.fdopen(fd, 'w', encoding='utf-8', newline="\n") as f:
                    for line in current + added:
                        f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(new_kh))
                tmp_path = None      # 差し替え済み。後片付けの対象から外す
            finally:
                if tmp_path is not None:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            print("[Config] known_hosts を ~/.terminal-tool から引き継ぎました")
        marker.write_text("", encoding="utf-8")
        return None
    except Exception as e:
        return ("旧 %s を引き継げませんでした（%s）。引き継げるまで、"
                "この機器は初回接続として扱われ、鍵が変わっていても"
                "気づけません。手でコピーしてください: %s"
                % (old_kh, e, new_kh))


def app_data_dir():
    """アプリのデータ保存先 (~/.netbelt) を返す。無ければ作る。

    known_hosts や SFTP サーバのホストキーなど、設定ファイルとは別に
    ユーザー単位で持ち回るものを置く。
    旧名 ~/.terminal-tool に known_hosts がある場合は引き継ぐ。引き継げ
    なかったときは take_known_hosts_import_warning() で理由を取り出せる。
    """
    global _known_hosts_import_warning
    new_dir = Path.home() / ".netbelt"
    try:
        new_dir.mkdir(exist_ok=True)
    except Exception:
        pass
    # 目印の確認から目印の作成までを、保存・読み込みと同じ錠の中で行う
    with known_hosts_lock:
        _known_hosts_import_warning = _import_legacy_known_hosts(new_dir)
    return new_dir


# 前回保存したフォルダの置き場所（settings.paths.last_save_dir）。
# 保存ダイアログを前回と同じ場所から開くためだけに使う（core/save_defaults.py）
_LAST_SAVE_DIR_SECTION = "paths"
_LAST_SAVE_DIR_KEY = "last_save_dir"


class ConfigManager:
    """設定ファイルの読み書きを管理するクラス"""
    
    def __init__(self, config_path: str = "config.json"):
        """
        初期化
        
        Args:
            config_path: 設定ファイルのパス
        """
        self.config_path = Path(config_path)
        self.crypto = PasswordCrypto()
        self.load_error = None  # 読み込みエラー情報
        self.load_warning = None  # 読み込めたが一部を除外したときの警告文
        self.backup_path = None  # バックアップファイルパス
        # 読み込み時に復号できなかった機器: 機器名 -> そのとき残った暗号文。
        # 接続の入口がここを見て、暗号文をパスワードとして送らないようにする
        self.undecryptable_devices: Dict[str, str] = {}
        # 同じ機器名が 2 グループにあると、上の辞書には後から読んだ方の
        # 暗号文しか残らない。値そのものでも見分けられるよう、読み込み時に
        # 出会った暗号文をここにも貯める
        self.undecryptable_values: Set[str] = set()
        # 直前のグループ操作が「保存だけ失敗した」のか「そもそも受け付け
        # られなかった（同名・対象なし）」のか。戻り値の False だけでは
        # 区別できず、呼び出し側が案内を書き分けられない
        self.last_save_failed = False
        self.config = self._load_config()
    
    def _load_config(self) -> Dict:
        """
        設定ファイルを読み込む
        
        Returns:
            設定データ
        """
        # 設定ファイルが存在しない場合はデフォルト設定を読み込む
        if not self.config_path.exists():
            config = self._load_default_config()
            # デフォルト設定をconfig.jsonとして保存
            self.config = config
            self.save_config()
            return config
        
        try:
            # utf-8-sig で読む。メモ帳の「UTF-8 (BOM)」や PowerShell 5.1 の
            # Out-File -Encoding utf8 で手編集すると先頭に BOM が付くが、
            # utf-8 のままだと BOM が文字として json.load へ渡り
            # 「Unexpected UTF-8 BOM」で設定全体が破損扱いになる（実測）。
            # その結果、機器もグループも消えた既定設定で起動し、次の保存で
            # config.json が既定設定へ置き換わっていた。BOM が無ければ
            # utf-8 と同じ。書き出しは今までどおり BOM 無しなので、一度
            # 読み直して保存すれば BOM は落ちる
            with open(self.config_path, 'r', encoding='utf-8-sig') as f:
                config = json.load(f)
                # name/host の無い機器とグループは先に整える（UI が KeyError で
                # 落ちる）。復号も機器が dict であることを前提にしているので、
                # 隔離はその前に済ませる
                self._quarantine_invalid_devices(config)
                # 重複した機器名・グループ名は除外まではしないが、知らせる
                # （除外された機器や、名前を補われたグループを数に入れない
                # よう、隔離のあとで見る）
                self._notify_duplicate_group_names(config)
                self._notify_duplicate_device_names(config)
                self._notify_duplicate_global_macro_names(config)
                # パスワードを復号化
                self._decrypt_passwords(config)
                self._notify_undecryptable()
            # Defaultグループが存在しない場合は追加（戻り値でフラグを受け取る）
            # 補いと保存は、読み込み用に開いたファイルを閉じてから行う。
            # save_config() は一時ファイルを os.replace で本体へ差し替えるが、
            # Windows では開いたままの本体へ差し替えると PermissionError
            # [WinError 5] になり、補った Default グループが毎回の起動で
            # 保存できないまま終わる（実測: 5 回中 5 回）
            need_save = self._ensure_default_group(config)
            # 一時的にconfigを設定して保存
            if need_save:
                self.config = config
                self.save_config()
            return config
        except Exception as e:
            print(f"[ERROR] 設定ファイルの読み込みエラー: {e}")
            # エラー情報を保存
            self.load_error = str(e)
            # 破損した設定ファイルをバックアップ
            self._backup_corrupted_config()
            # デフォルト設定を返す（既存ファイルは上書きしない）
            print("[INFO] デフォルト設定を使用します。次回保存時に設定ファイルが作成されます。")
            return self._load_default_config()
    
    def _load_default_config(self) -> Dict:
        """
        デフォルト設定を読み込む
        
        Returns:
            デフォルト設定データ
        """
        default_path = Path(__file__).parent.parent / "resources" / "default_config.json"
        
        try:
            with open(default_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"デフォルト設定ファイルの読み込みエラー: {e}")
            # フォールバック: 最小限の設定を返す
            return {
                "config_version": "1.0",
                "groups": [],
                "global_macros": [],
                "settings": {
                    "terminal": {
                        "background_color": "#000000",
                        "text_color": "#FFFFFF",
                        "font_family": "Consolas",
                        "font_size": 10
                    }
                },
                "update_settings": {
                    "check_on_startup": True,
                    "skipped_version": None,
                    "last_check": None,
                    "github_token": None
                }
            }
    
    # 機器のパスワードと同じく暗号化して保存する settings のセクション。
    # 画面では伏字にしているのにディスクは平文、という状態を避ける。
    _ENCRYPTED_SETTING_SECTIONS = ("ftp_server", "sftp_server")

    # 「その画面で入れ直してください」と案内してよいセクション。
    # 入れ直せるのは、settings のパスワードを実際に読み書きしている画面が
    # あるものだけ。FTPServerPanel は _restore_settings() /
    # set_server_settings() で settings.ftp_server を読み書きするが、
    # SFTPServerPanel は settings.sftp_server を読みも書きもしない
    # （config_manager は受け取るが、使うのは保存先のフォルダの記憶だけ）。
    # 案内どおりパネルを開いても設定ファイルのその値を直す場所が無いので、
    # ここには入れない。
    # 暗号化の対象（_ENCRYPTED_SETTING_SECTIONS）からは外さない。手で置かれた
    # 平文をディスクへ残さない性質は、読まれない値でも変えない。
    # SFTP サーバーパネルが settings を読むようになったら sftp_server をここへ戻す
    # （tests/test_sftp_server_password_notice_target.py が、そのパネルが
    # get_server_settings / set_server_settings を呼んでいないかで見張っている）。
    _SETTING_SECTIONS_WITH_EDITOR = ("ftp_server",)

    # 復号できなかったときに、どのタブを開けばよいか伝えるための表示名
    _SETTING_SECTION_LABELS = {
        "ftp_server": "FTPサーバー",
        "sftp_server": "SFTPサーバー",
    }

    def _encrypt_passwords(self, config: Dict) -> None:
        """
        設定内の全パスワードを暗号化
        
        Args:
            config: 設定データ
        """
        for group in config.get("groups", []):
            for device in group.get("devices", []):
                password = device.get("password", "")
                # 既に暗号化済みの値は再暗号化しない。DPAPI の鍵は Windows
                # アカウント/マシンに紐づくため、config.json を別環境へ移すと
                # decrypt() が暗号文をそのまま返す。それを平文とみなして再度
                # 暗号化すると二重に包まれ、元のパスワードが復元不能になる。
                # 保存は起動時にも走るので、ここで弾かないと原本が失われる。
                if password and not self.crypto.is_encrypted(password):
                    device["password"] = self.crypto.encrypt(password)

        # GitHub トークンも資格情報。UI からは設定できないが、手で置かれた
        # 平文をディスク（と破損時の backup_*）に残さない。
        # 暗号化済みを触らない理由は機器側と同じ
        update_settings = config.get("update_settings")
        if isinstance(update_settings, dict):
            token = update_settings.get("github_token")
            if (isinstance(token, str) and token
                    and not self.crypto.is_encrypted(token)):
                update_settings["github_token"] = self.crypto.encrypt(token)

        settings = config.get("settings")
        if not isinstance(settings, dict):
            return
        for name in self._ENCRYPTED_SETTING_SECTIONS:
            section = settings.get(name)
            if not isinstance(section, dict):
                continue
            password = section.get("password", "")
            # 機器側と同じ理由で、暗号化済みは触らない
            if (isinstance(password, str) and password
                    and not self.crypto.is_encrypted(password)):
                section["password"] = self.crypto.encrypt(password)
    
    def _decrypt_passwords(self, config: Dict) -> None:
        self._undecryptable_count = 0
        self._undecryptable_settings: List[str] = []
        """
        設定内の全パスワードを復号化
        
        Args:
            config: 設定データ
        """
        for group in config.get("groups", []):
            for device in group.get("devices", []):
                encrypted = device.get("password", "")
                if not encrypted:
                    continue
                decrypted = self.crypto.decrypt(encrypted)
                # 復号に失敗すると入力がそのまま返る。値は保持したまま件数を数える
                # （原本の保護は _encrypt_passwords 側で行う）
                # 本物の DPAPI 暗号文だけを覚え、数える。"DPAPI:cisco123" の
                # ような平文の合言葉まで覚えると、接続の入口がそれを暗号文と
                # みなして、正しいパスワードの機器を断る。数だけ入れても、
                # 案内の件数と実際に断られる機器の数が食い違う
                if self.crypto.is_dpapi_ciphertext(decrypted):
                    self._undecryptable_count += 1
                    # 名前は上書きされうるが、値は同名の機器が何台あっても
                    # それぞれ残る
                    self.undecryptable_values.add(decrypted)
                    name = device.get("name")
                    if isinstance(name, str) and name:
                        self.undecryptable_devices[name] = decrypted
                device["password"] = decrypted

        # トークンは件数に数えない。復号できないときの案内は
        # get_github_token() が出す（文面が機器のパスワードと違う）
        update_settings = config.get("update_settings")
        if isinstance(update_settings, dict):
            token = update_settings.get("github_token")
            if isinstance(token, str) and token:
                update_settings["github_token"] = self.crypto.decrypt(token)

        settings = config.get("settings")
        if not isinstance(settings, dict):
            return
        for name in self._ENCRYPTED_SETTING_SECTIONS:
            section = settings.get(name)
            if not isinstance(section, dict):
                continue
            encrypted = section.get("password", "")
            if not isinstance(encrypted, str) or not encrypted:
                continue
            decrypted = self.crypto.decrypt(encrypted)
            # 機器と違い、内蔵サーバは is_encrypted に当たる値のままでは
            # 起動を断られる（平文の合言葉でも同じ）。断られる条件に
            # そろえて、ここは is_encrypted で数える
            if self.crypto.is_encrypted(decrypted):
                self._undecryptable_settings.append(name)
            section["password"] = decrypted
    
    def _notify_undecryptable(self) -> None:
        """復号できなかったパスワードがあれば知らせる。

        print だけだと、exe から起動した利用者には何も見えない。接続を
        断られてから理由を探すことになるので、起動時の警告（load_warning）
        にも載せて画面に出す。

        機器と内蔵サーバでは入れ直す場所が違う。ひとまとめに数えて
        「機器の編集で入れ直してください」と出すと、機器が 1 台も絡まない
        ときにも機器の話をしてしまい、開いても直すところが無い。由来ごとに
        分けて数え、書き分ける。

        内蔵サーバの中でも、その画面が settings を読み書きしているもの
        （_SETTING_SECTIONS_WITH_EDITOR）だけに入れ直しを案内する。
        読まれない値は直す場所が無く、起動を断られる原因にもならないので、
        知らせるだけにして「動作には影響しない」と書く。
        """
        devices = getattr(self, "_undecryptable_count", 0)
        sections = getattr(self, "_undecryptable_settings", [])
        parts = []
        if devices:
            parts.append(f"{devices}件の機器のパスワードを復号できませんでした。"
                         "該当機器のパスワードは、機器の編集で入れ直してください。")
        editable = [n for n in sections
                    if n in self._SETTING_SECTIONS_WITH_EDITOR]
        unused = [n for n in sections
                  if n not in self._SETTING_SECTIONS_WITH_EDITOR]
        if editable:
            parts.append(f"{self._section_labels(editable)}の"
                         "パスワードを復号できませんでした。"
                         "その画面で入れ直してください"
                         "（表示メニューから開けます）。")
        if unused:
            parts.append(f"{self._section_labels(unused)}の"
                         "パスワードを復号できませんでした。"
                         "この版では設定ファイルのこの値を読まないため、"
                         "サーバーの動作には影響しません"
                         "（その画面で入力したパスワードが使われます）。")
        if not parts:
            return
        self._append_load_warning(
            "\n".join(parts)
            + "\n別の Windows アカウント/PC で保存された設定の可能性があります。"
              "（設定ファイル内の元の値は保護されており、上書きされません）")

    def _section_labels(self, names) -> str:
        """settings のセクション名を、画面に出す表示名へ並べ直す。"""
        return "、".join(self._SETTING_SECTION_LABELS.get(name, name)
                        for name in names)

    def _append_load_warning(self, message: str) -> None:
        """起動時の警告を書き足す（先に記録された警告を消さない）。"""
        print(f"[Config] {message}")
        self.load_warning = (f"{self.load_warning}\n\n{message}"
                             if self.load_warning else message)

    def _notify_duplicate_group_names(self, config: Dict) -> None:
        """同じ名前のグループが複数ある設定を読んだら、その名前を挙げて知らせる。

        グループもまた名前だけで探す（get_group は先頭の 1 件を返す）ので、
        2 つ目以降の同名グループに入っている機器には update_device /
        remove_device / move_device のどれも届かない。画面には並んでいるのに
        操作だけが黙って失敗するため、機器名の重複と同じく名指しで知らせる。

        黙って除外・改名すると利用者の機器やグループが消えるので、設定は
        書き換えない。直し方は画面から案内する。rename_group も get_group
        経由で 1 つ目しか掴まないが、その 1 つ目を別の名前にすれば重複は
        解けるので、接続先リストの「グループを編集」で直せる（実測）。
        変わるのが先に並んでいる方だということも添える。
        """
        seen = set()
        duplicates = []
        for group in config.get("groups", []):
            if not isinstance(group, dict):
                continue
            name = group.get("name")
            if not isinstance(name, str) or not name:
                continue
            if name not in seen:
                seen.add(name)
            elif name not in duplicates:
                duplicates.append(name)
        if not duplicates:
            return
        self._append_load_warning(
            "設定ファイル (config.json) に同じ名前のグループが複数あります: "
            + "、".join(duplicates)
            + "\nグループも名前で探すため、2 つ目以降の同名グループにある機器は"
              "編集も削除も移動もできません。接続先リストでどちらかのグループを"
              "右クリックし「グループを編集」で別の名前にすると分かれます"
              "（名前で探すため、変わるのは先に並んでいる方です）。"
              "config.json を直接直しても構いません。")

    def _notify_duplicate_device_names(self, config: Dict) -> None:
        """同じ名前の機器が複数ある設定を読んだら、その名前を挙げて知らせる。

        機器名は全グループを通して一意である前提で、接続の管理も所属グループの
        検索も名前だけで行う（add_device / update_device は重複を断る）。
        しかし読み込みは重複を弾かないので、手編集・他ツール由来・他 PC から
        持ち込んだ config.json では、どちらの機器が選ばれるか決まらないまま
        動くことになる。黙って除外すると利用者の機器が消えるので、消さずに
        名前を挙げるだけにする。
        """
        seen = set()
        duplicates = []
        for group in config.get("groups", []):
            if not isinstance(group, dict):
                continue
            for device in group.get("devices", []):
                if not isinstance(device, dict):
                    continue
                name = device.get("name")
                if not isinstance(name, str) or not name:
                    continue
                if name not in seen:
                    seen.add(name)
                elif name not in duplicates:
                    duplicates.append(name)
        if not duplicates:
            return
        self._append_load_warning(
            "設定ファイル (config.json) に同じ名前の機器が複数あります: "
            + "、".join(duplicates)
            + "\n接続も設定の操作も機器名で相手を探すため、どちらが選ばれるかは"
              "決まりません。機器の編集で名前を分けてください。")

    def _notify_duplicate_global_macro_names(self, config: Dict) -> None:
        """同じ名前の共通マクロが複数ある設定を読んだら、その名前を挙げて知らせる。

        GUI の新規作成は同名を断るが、読み込みは重複を弾かない。実行・編集・
        削除は名前で相手を引くので、どれを指すかが決まらない（count_macros_named
        を参照）。その名前の操作は断ることにしたので、ここで理由と直し方を
        先に伝える。名前は画面から変えられない（編集では名前欄が読み取り
        専用）ので、config.json を直すよう案内する。黙って除外・改名は
        しない（利用者のコマンド列が消える）。
        """
        seen = set()
        duplicates = []
        macros = config.get("global_macros")
        for macro in macros if isinstance(macros, list) else []:
            if not is_readable_macro(macro):
                continue
            name = macro["name"]
            if name not in seen:
                seen.add(name)
            elif name not in duplicates:
                duplicates.append(name)
        if not duplicates:
            return
        self._append_load_warning(
            "設定ファイル (config.json) に同じ名前の全体共通マクロ（プリセット）が"
            "複数あります: " + "、".join(duplicates)
            + "\nマクロは名前で探すため、どれを指すかが決まりません。機器へ"
              "別のマクロを送らないよう、この名前のマクロは実行・編集・削除を"
              "断ります。config.json を直接編集して名前を分けてください。")

    def has_undecryptable_password(self, device_name, password) -> bool:
        """その機器のパスワードが、読み込み時に復号できなかった暗号文のままか。

        接続の入口が使う。is_encrypted の推測ではなく読み込み時の記録で
        見分けるので、機器へ本当に "DPAPI:..." を設定している場合や、
        この画面で入れ直したばかりの値を断ることはない。

        名前の記録だけでは、同じ名前の機器が 2 グループにあるときに片方
        しか覚えられない（手編集や他 PC 由来の config.json では重複が
        読み込まれる）。値そのものとも照合して、どちらの機器も断る。
        入れ直した新しいパスワードは読み込み時の集合に入らないので、
        「編集で断られなくなる」性質は変わらない。
        """
        recorded = self.undecryptable_devices.get(device_name)
        if recorded and password == recorded:
            return True
        return isinstance(password, str) and password in self.undecryptable_values

    def _refresh_undecryptable(self, old_name: str, device_info: Dict) -> None:
        """機器を差し替えたあとの記録を合わせる。

        パスワードを入れ直していれば記録を外す。名前だけ変えて暗号文を
        そのまま残した場合は、新しい名前へ付け替える。
        """
        recorded = self.undecryptable_devices.pop(old_name, None)
        new_name = device_info.get("name")
        if (recorded is not None and isinstance(new_name, str) and new_name
                and device_info.get("password") == recorded):
            self.undecryptable_devices[new_name] = recorded

    @staticmethod
    def _is_valid_device(device) -> bool:
        """UI が前提にする必須フィールド（name/host が空でない文字列）を持つか。"""
        return (isinstance(device, dict)
                and isinstance(device.get("name"), str) and bool(device["name"])
                and isinstance(device.get("host"), str) and bool(device["host"]))

    @staticmethod
    def _is_valid_auto_commands(commands) -> bool:
        """接続時に自動送信できる形（文字列だけの list）かを返す。

        手編集や他ツールの config.json には "auto_commands": "show version"
        のような値が混ざる。送信側は list(commands) にしてから 1 要素ずつ
        CR を付けて送るので、文字列は 1 文字ずつのコマンドに化け、辞書は
        キーだけが送られる。list() できない値（数値など）は送信を仕掛ける
        QTimer のコールバックの中で TypeError になり、PyQt6 はスロット内の
        未捕捉例外で終了するため、接続した瞬間にアプリごと落ちる。
        """
        return (isinstance(commands, list)
                and all(isinstance(c, str) for c in commands))

    @staticmethod
    def _normalize_optional_list(container: Dict, key: str, keep) -> bool:
        """任意項目のリストをそろえる。読めない分を外したら True を返す。

        機器の macros とグループの auto_commands は無くてもよい項目だが、
        読み手は list であることを前提にしている（for で回す・list() で
        写す）。手編集や他ツールの config.json に null や別の型が入ると、
        一覧には出るのに編集ダイアログが例外で開かなくなり、その機器・
        グループは編集で直すこともパスワードを入れ直すこともできない。

        null は「無し」と同じ意味なので、黙って空のリストにそろえる
        （パスワードの null と同じ扱い）。ほかの型と、リストの中の読めない
        要素は、中身を失う直し方なので name / host の隔離と同じく知らせる。

        Args:
            container: 機器またはグループの辞書
            key: そろえる項目名
            keep: 残してよい要素かを答える関数
        """
        value = container.get(key, [])
        if value is None:
            container[key] = []
            return False
        if not isinstance(value, list):
            container[key] = []
            return True
        kept = [item for item in value if keep(item)]
        if len(kept) == len(value):
            return False
        container[key] = kept
        return True

    @staticmethod
    def _normalize_password(device) -> Optional[str]:
        """機器のパスワード欄を文字列にそろえる。直したら種別を返す。

        手編集や他ツールの config.json には "password": 1234 のような値が
        混ざる。復号は文字列を前提にしていて（startswith）、真値の非文字列
        は AttributeError になり、_load_config の全体の例外処理が掴んで
        設定全体を既定値へ差し替えてしまう。機器 1 台の型のために全機器と
        全マクロを失うのは割に合わないので、ここで型だけそろえて残す。

        数値は入力されたパスワードとして読めるので str() で文字列にする
        （保存時は他と同じく暗号化される）。bool は数値として扱わない
        （"True" はパスワードではない）。null は空文字と同じ「パスワード
        なし」なので、黙ってそろえるだけで警告しない。
        """
        password = device.get("password", "")
        if isinstance(password, str):
            return None
        if password is None:
            device["password"] = ""
            return None
        if isinstance(password, (int, float)) and not isinstance(password, bool):
            device["password"] = str(password)
            return "numeric"
        device["password"] = ""
        return "emptied"

    # 機器の任意文字列項目と、読めないときに戻す既定値・画面へ出す名前。
    # 読み手（DeviceDialog）は QLineEdit.setText / QComboBox.findText へ
    # そのまま渡すので、文字列でないと編集ダイアログが TypeError で開けず、
    # その機器は GUI から二度と直せなくなる（パスワードの入れ直しも不可）。
    # password は暗号化の都合があるので _normalize_password が別に見る
    _DEVICE_TEXT_FIELDS = (
        ("username", "", "ユーザー名"),
        ("ssh_key", "", "秘密鍵"),
        ("protocol", "ssh", "プロトコル"),
    )

    @staticmethod
    def _normalize_text_field(device, key, default):
        """機器の任意文字列項目を文字列にそろえる。直したら種別を返す。

        パスワードと同じ流儀にそろえる。数値は入力された値として読める
        ので str() で文字列にし、それ以外の型（list / dict / bool）は
        中身を失う直し方なので既定値へ戻して知らせる。null は「無し」と
        同じ意味なので、黙ってそろえるだけで警告しない。
        キーごと無いのは「未設定」なので足さない。
        """
        if key not in device:
            return None
        value = device[key]
        if isinstance(value, str):
            return None
        if value is None:
            device[key] = default
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            device[key] = str(value)
            return "numeric"
        device[key] = default
        return "emptied"

    # name の無いグループに与える表示名。グループごと捨てると中の正常な
    # 機器まで消えるので、名前だけ補って中身は残す
    UNNAMED_GROUP_NAME = "(名前なし)"

    def _unused_group_name(self, used_names) -> str:
        """まだ使われていない「(名前なし)」系の表示名を返す。

        同名のグループを作ってはいけない。グループは名前で指すため、
        get_group() も remove_group() も先頭の 1 件しか掴まない（実測。
        remove_group() が同名をすべて消していたのは直す前の話で、いまは
        tests/test_config_unnamed_group_removal.py が 1 件だけであることを
        押さえている）。同じ補完名が並ぶと、2 つ目以降のグループには編集も
        削除も届かず、消したつもりの操作は毎回 1 つ目に当たる。
        """
        name = self.UNNAMED_GROUP_NAME
        number = 1
        while name in used_names:
            number += 1
            name = f"{self.UNNAMED_GROUP_NAME} {number}"
        return name

    def _quarantine_invalid_devices(self, config: Dict) -> None:
        """必須フィールドの無い機器とグループを整えて、警告を記録する。

        手編集や他ツールで作られた config.json に {} や {"host": ...} の
        ような機器が混ざると、DeviceTree の構築が KeyError で落ちて
        起動できない。JSON 構文エラーとは違い load_error にもならないので、
        利用者には設定ファイルが原因だと分からなかった。
        グループ側も同じで、DeviceTree は group_data["name"] を直接引くため
        name の無いグループでも同じ KeyError で起動できない。
        不正な機器だけを除外し、名前の無いグループには表示名を補い、
        グループとして読めない項目は外して、元ファイルはバックアップして
        知らせる。

        予約語（ホームタブと重なる名前）の機器も同じ理由でここで外す。
        登録の入口（DeviceDialog / add_device / update_device）は別途
        断っているが、手編集された config や、その検査より前のバージョン
        で作られた config からは今でも入ってくる。
        """
        removed = 0
        reserved = 0
        renamed_groups = 0
        dropped_groups = 0
        emptied_groups = 0
        numeric_passwords = []   # 文字列に直した機器名
        emptied_passwords = []   # パスワードを空にした機器名
        numeric_texts = []       # 文字列に直した「機器名 の 項目名」
        emptied_texts = []       # 既定値へ戻した「機器名 の 項目名」
        broken_macros = []       # 読めないマクロを外した機器名
        broken_auto_commands = []  # 自動実行コマンドを無効にしたグループ名
        kept_groups = []
        # 手で付けられた名前とも衝突させない
        used_names = {g["name"] for g in config.get("groups", [])
                      if isinstance(g, dict) and isinstance(g.get("name"), str)}
        for group in config.get("groups", []):
            if not isinstance(group, dict):
                # 名前も機器も取り出せないので、この項目は諦めるしかない
                dropped_groups += 1
                continue
            name = group.get("name")
            if not isinstance(name, str) or not name:
                group["name"] = self._unused_group_name(used_names)
                used_names.add(group["name"])
                renamed_groups += 1
            # 自動実行コマンドの形。接続しただけで実機へ流れるので、読めない
            # 形なら一覧ごと無効にして知らせる（利用者が書いていない操作を
            # 送るより安全）。キーごと無いのは「自動実行なし」なので触らない。
            # null も「無し」と同じ意味なので、黙って空のリストにそろえる
            # （グループの編集ダイアログは list() で写すので、null では開けない）
            if "auto_commands" in group:
                if group["auto_commands"] is None:
                    group["auto_commands"] = []
                elif not self._is_valid_auto_commands(group["auto_commands"]):
                    group["auto_commands"] = []
                    broken_auto_commands.append(group["name"])
            devices = group.get("devices")
            if isinstance(devices, list):
                kept = []
                for d in devices:
                    if not self._is_valid_device(d):
                        removed += 1
                    elif is_reserved_device_name(d["name"]):
                        reserved += 1
                    else:
                        fixed = self._normalize_password(d)
                        if fixed == "numeric":
                            numeric_passwords.append(d["name"])
                        elif fixed == "emptied":
                            emptied_passwords.append(d["name"])
                        # ユーザー名・秘密鍵・プロトコルも、読み手が文字列を
                        # 前提にしている（setText / findText）
                        for key, default, label in self._DEVICE_TEXT_FIELDS:
                            fixed = self._normalize_text_field(d, key, default)
                            if fixed == "numeric":
                                numeric_texts.append(f"{d['name']} の{label}")
                            elif fixed == "emptied":
                                emptied_texts.append(f"{d['name']} の{label}")
                        # 機器別マクロも、機器の編集ダイアログが for で回すので
                        # list であることが前提（要素は名前を持つ辞書）
                        if self._normalize_optional_list(
                                d, "macros", is_readable_macro):
                            broken_macros.append(d["name"])
                        kept.append(d)
                group["devices"] = kept
            else:
                # devices の欠落・辞書・None。この先は list であることが前提で、
                # add_device は group["devices"] で KeyError、_decrypt_passwords は
                # 辞書や None を回そうとして読み込み自体を失敗させる。
                # 名前と同じく、入れ物も空で補って中身の無いグループとして扱う
                group["devices"] = []
                emptied_groups += 1
            kept_groups.append(group)
        # 3 つ目の任意リスト。機器の macros と同じく、読み手は list である
        # ことを前提にしている（for で回す・list() で写す）。null のまま
        # 残ると、接続中の機器の「ツール」を作るだけで落ちる
        broken_global_macros = self._normalize_optional_list(
            config, "global_macros", is_readable_macro)
        if dropped_groups:
            config["groups"] = kept_groups
        if not (removed or reserved or renamed_groups or dropped_groups
                or emptied_groups or numeric_passwords or emptied_passwords
                or numeric_texts or emptied_texts
                or broken_macros or broken_auto_commands
                or broken_global_macros):
            return
        self._backup_corrupted_config()
        parts = []
        reasons = []
        if removed:
            reasons.append(f"名前またはホストの無い機器が{removed}件")
        if reserved:
            reasons.append(f"ホームタブと重なる名前（「ホーム」）の機器が{reserved}件")
        if reasons:
            parts.append("設定ファイル (config.json) に" + "、".join(reasons) +
                         "あり、接続先リストから除外しました。\n"
                         "除外した機器は次回の保存時に設定ファイルから消えます。")
        if renamed_groups:
            parts.append(f"設定ファイル (config.json) に名前の無いグループが"
                         f"{renamed_groups}件あり、"
                         f"「{self.UNNAMED_GROUP_NAME}」として表示します。")
        if dropped_groups:
            parts.append(f"設定ファイル (config.json) にグループとして読めない項目が"
                         f"{dropped_groups}件あり、除外しました。")
        if emptied_groups:
            parts.append(f"設定ファイル (config.json) に機器一覧の形が壊れたグループが"
                         f"{emptied_groups}件あり、機器の無いグループとして扱います。")
        if numeric_passwords:
            parts.append("設定ファイル (config.json) でパスワードが数値になっていた"
                         "機器があり、そのまま文字列として扱います: "
                         + "、".join(numeric_passwords))
        if emptied_passwords:
            parts.append("設定ファイル (config.json) でパスワードが文字列でない"
                         "機器があり、パスワードを空にしました。"
                         "機器の編集で入れ直してください: "
                         + "、".join(emptied_passwords))
        if numeric_texts:
            parts.append("設定ファイル (config.json) で数値になっていた項目があり、"
                         "そのまま文字列として扱います: "
                         + "、".join(numeric_texts))
        if emptied_texts:
            parts.append("設定ファイル (config.json) で文字列でない項目があり、"
                         "既定値に戻しました。機器の編集で入れ直してください: "
                         + "、".join(emptied_texts))
        if broken_macros:
            parts.append("設定ファイル (config.json) でマクロの一覧が読めない機器が"
                         "あり、そのマクロを外しました。機器の編集で入れ直して"
                         "ください: " + "、".join(broken_macros))
        if broken_global_macros:
            parts.append("設定ファイル (config.json) で全体共通マクロの一覧が"
                         "読めなかったため、読めない分を外しました。"
                         "マクロ設定の「プリセット管理」で入れ直してください。")
        if broken_auto_commands:
            parts.append("設定ファイル (config.json) で自動実行コマンドが"
                         "文字列の配列になっていないグループがあり、"
                         "そのグループの自動実行を無効にしました。"
                         "グループの編集で入れ直してください: "
                         + "、".join(broken_auto_commands))
        message = "\n".join(parts)
        if self.backup_path:
            message += f"\n\n元のファイルはバックアップしました:\n  {self.backup_path}"
        self.load_warning = message
        print(f"[Config] 機器{removed + reserved}件を除外 "
              f"(name/host 無し={removed}, 予約語={reserved})、"
              f"グループ{renamed_groups}件を改名、グループ{dropped_groups}件を除外、"
              f"グループ{emptied_groups}件の機器一覧を空にしました "
              f"(パスワードを文字列化={len(numeric_passwords)}, "
              f"空にした={len(emptied_passwords)}, "
              f"マクロを外した={len(broken_macros)}, "
              f"項目を文字列化={len(numeric_texts)}, "
              f"項目を既定値に戻した={len(emptied_texts)}, "
              f"自動実行を無効にしたグループ={len(broken_auto_commands)})")

    def _backup_corrupted_config(self) -> None:
        """
        破損した設定ファイルをバックアップ
        """
        try:
            if not self.config_path.exists():
                return
            
            # タイムスタンプ付きバックアップファイル名を生成
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = self.config_path.with_name(f"config.json.backup_{timestamp}")
            
            # バックアップを作成
            import shutil
            shutil.copy2(self.config_path, backup_path)
            self.backup_path = str(backup_path)  # バックアップパスを保存
            print(f"[INFO] 破損した設定ファイルをバックアップしました: {backup_path}")
            
            # 古いバックアップファイルを整理（最新5つを保持）
            self._cleanup_old_backups()
            
        except Exception as e:
            print(f"[WARNING] 設定ファイルのバックアップに失敗しました: {e}")
    
    def _cleanup_old_backups(self, keep_count: int = 5) -> None:
        """
        古いバックアップファイルを削除
        
        Args:
            keep_count: 保持するバックアップ数
        """
        try:
            # バックアップファイルを検索
            backup_pattern = "config.json.backup_*"
            backup_dir = self.config_path.parent
            backup_files = sorted(backup_dir.glob(backup_pattern), reverse=True)
            
            # 古いバックアップを削除
            for backup_file in backup_files[keep_count:]:
                backup_file.unlink()
                print(f"[INFO] 古いバックアップを削除しました: {backup_file.name}")
                
        except Exception as e:
            print(f"[WARNING] バックアップファイルの整理に失敗しました: {e}")
    
    def _ensure_default_group(self, config: Dict) -> bool:
        """
        Defaultグループが存在しない場合は追加する
        
        Args:
            config: 設定データ
            
        Returns:
            Defaultグループを追加した場合True、既に存在する場合False
        """
        groups = config.get("groups", [])
        
        # Defaultグループが存在するかチェック
        has_default = any(g.get("name") == "Default" for g in groups)
        
        if not has_default:
            # Defaultグループを先頭に追加
            default_group = {
                "name": "Default",
                "auto_commands": [],
                "devices": []
            }
            groups.insert(0, default_group)
            config["groups"] = groups
            print("[INFO] Defaultグループを追加しました")
            return True
        
        return False
    
    def save_config(self) -> bool:
        """
        設定ファイルを保存
        
        Returns:
            保存成功時True、失敗時False
        """
        try:
            # 保存用に設定をコピー（パスワードを暗号化）
            save_config = json.loads(json.dumps(self.config))
            self._encrypt_passwords(save_config)
            
            # 本体を直接開くと、その瞬間に長さ 0 へ切り詰められる。書き終える
            # までに落ちると（強制終了・シャットダウン・ディスク満杯）全機器と
            # パスワードが消え、次回起動時のバックアップにも残骸しか入らない。
            # 同じディレクトリへ書いてから os.replace で差し替える。差し替えは
            # 不可分なので、失敗しても前の config.json がそのまま残る。
            # 一時ファイルを同階層に作るのは、os.replace がドライブを跨げないため。
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(self.config_path.parent),
                    prefix=self.config_path.name + ".",
                    suffix=".tmp")
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(save_config, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(self.config_path))
                tmp_path = None      # 差し替え済み。後片付けの対象から外す
            finally:
                if tmp_path is not None:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

            return True
        except Exception as e:
            print(f"設定ファイルの保存エラー: {e}")
            return False
    
    def get_groups(self) -> List[Dict]:
        """グループ一覧を取得"""
        return self.config.get("groups", [])
    
    def get_group(self, group_name: str) -> Optional[Dict]:
        """指定したグループを取得"""
        for group in self.config.get("groups", []):
            if group["name"] == group_name:
                return group
        return None
    
    def add_group(self, group_name: str, auto_commands: List[str] = None) -> bool:
        """
        グループを追加
        
        Args:
            group_name: グループ名
            auto_commands: 自動実行コマンド
            
        Returns:
            追加成功時True、失敗時False
        """
        self.last_save_failed = False
        # 同名グループが存在しないかチェック
        if self.get_group(group_name):
            return False

        # 読み込み・set_group_auto_commands と同じ検査をここでも行う。
        # ここだけ素通しにすると、そのセッションの間は
        # MainWindow._run_auto_commands が壊れた値をそのまま list() して
        # 送るので、接続した瞬間に 1 文字ずつが実機へ届く（実測）。
        # None や空の値は「自動実行なし」なので今までどおり [] にそろえる
        commands = auto_commands or []
        if not self._is_valid_auto_commands(commands):
            print(f"エラー: グループ '{group_name}' の自動実行コマンドは"
                  f"文字列の配列で指定してください")
            return False

        new_group = {
            "name": group_name,
            "auto_commands": commands,
            "devices": []
        }

        groups = self.config["groups"]
        groups.append(new_group)
        if self.save_config():
            return True
        # 保存できなかったのにメモリへ残すと、次の無関係な保存（終了時の
        # レイアウト保存など）で、失敗と案内した追加がディスクに確定する
        if groups and groups[-1] is new_group:
            groups.pop()
        self.last_save_failed = True
        return False
    
    def remove_group(self, group_name: str) -> bool:
        """
        グループを削除
        
        Args:
            group_name: グループ名
            
        Returns:
            削除成功時True、失敗時False（対象のグループが無いときも False）
        """
        # 消すのは get_group() が返すのと同じ 1 件だけ。同名のグループが
        # あるとき全部消すと、UI が「機器が含まれていません」と確認した
        # グループを消したつもりで、同名の別グループの機器まで消える
        self.last_save_failed = False
        groups = self.config.get("groups", [])
        for index, group in enumerate(groups):
            if group.get("name") == group_name:
                del groups[index]
                break
        else:
            # 何も消していないので保存もしない。保存結果の True を返すと、
            # 呼び出し側が「削除しました」と案内してしまう
            return False
        if self.save_config():
            return True
        groups.insert(index, group)   # 保存できなかったらメモリも戻す
        self.last_save_failed = True
        return False
    
    def rename_group(self, old_name: str, new_name: str) -> bool:
        """
        グループ名を変更
        
        Args:
            old_name: 変更前のグループ名
            new_name: 変更後のグループ名
            
        Returns:
            変更成功時True、失敗時False
        """
        self.last_save_failed = False
        # 変更対象のグループを取得
        group = self.get_group(old_name)
        if not group:
            print(f"エラー: グループ '{old_name}' が見つかりません")
            return False

        # 新しい名前が既に存在しないかチェック
        if self.get_group(new_name):
            print(f"エラー: グループ '{new_name}' は既に存在します")
            return False

        # グループ名を変更
        group["name"] = new_name

        # 設定を保存
        if self.save_config():
            print(f"[INFO] グループ名を '{old_name}' から '{new_name}' に変更しました")
            return True
        # 保存できなかったらメモリも戻す。旧名のまま残るので、その
        # グループの自動実行コマンドも付いたまま残る
        group["name"] = old_name
        self.last_save_failed = True
        return False
    
    def set_group_auto_commands(self, group_name: str, commands: List[str]) -> bool:
        """
        グループの自動実行コマンドを設定

        get_group() が返す参照に依存せず、groups を自分で走査して書き換える。
        将来 get_group() がコピーを返すようになっても壊れないようにするため。

        Args:
            group_name: グループ名
            commands: 自動実行コマンドのリスト（空リスト可）

        Returns:
            設定成功時True、グループが無ければFalse。
            commands が文字列だけの list でないときも False（保存しない）
        """
        self.last_save_failed = False
        # 文字列を渡されると list() が 1 文字ずつに分解する。そのまま保存すると
        # 次の接続で 1 文字ずつが実機へ送られるので、書き込む前に断る
        if not self._is_valid_auto_commands(commands):
            print(f"エラー: グループ '{group_name}' の自動実行コマンドは"
                  f"文字列の配列で指定してください")
            return False
        missing = object()
        for group in self.config.get("groups", []):
            if group.get("name") == group_name:
                before = group.get("auto_commands", missing)
                group["auto_commands"] = list(commands)
                if self.save_config():
                    return True
                # 保存できなかったらメモリも戻す（元から無ければ無い状態へ）
                if before is missing:
                    del group["auto_commands"]
                else:
                    group["auto_commands"] = before
                self.last_save_failed = True
                return False

        print(f"エラー: グループ '{group_name}' が見つかりません")
        return False
    
    def get_update_settings(self) -> Dict:
        """
        更新設定を取得
        
        Returns:
            更新設定の辞書
        """
        if "update_settings" not in self.config:
            # 更新設定が存在しない場合はデフォルトを追加
            self.config["update_settings"] = {
                "check_on_startup": True,
                "skipped_version": None,
                "last_check": None,
                "github_token": None
            }
            self.save_config()
        
        stored = self.config.get("update_settings")
        return stored if isinstance(stored, dict) else {}
    
    def update_update_settings(self, settings: Dict) -> bool:
        """
        更新設定を更新
        
        Args:
            settings: 更新する設定
        
        Returns:
            更新成功時True、失敗時False
        """
        if not isinstance(self.config.get("update_settings"), dict):
            self.config["update_settings"] = {}
        
        self.config["update_settings"].update(settings)
        return self.save_config()
    
    def get_check_on_startup(self) -> bool:
        """起動時の更新チェック設定を取得

        手編集で bool 以外が入っていても bool を返す。生値を
        QCheckBox.setChecked() へ渡すと TypeError になるため。
        """
        value = self.get_update_settings().get("check_on_startup", True)
        return value if isinstance(value, bool) else True
    
    def set_check_on_startup(self, enabled: bool) -> bool:
        """起動時の更新チェック設定を変更"""
        return self.update_update_settings({"check_on_startup": enabled})
    
    def get_skipped_version(self) -> Optional[str]:
        """スキップ中のバージョンを取得（文字列でなければ None）"""
        value = self.get_update_settings().get("skipped_version")
        return value if isinstance(value, str) and value else None
    
    def set_skipped_version(self, version: Optional[str]) -> bool:
        """スキップするバージョンを設定"""
        return self.update_update_settings({"skipped_version": version})
    
    def get_last_check_time(self) -> Optional[str]:
        """最終更新チェック時刻を取得"""
        return self.get_update_settings().get("last_check")
    
    def set_last_check_time(self, timestamp: str) -> bool:
        """最終更新チェック時刻を設定"""
        return self.update_update_settings({"last_check": timestamp})
    
    def get_github_token(self) -> Optional[str]:
        """GitHubトークンを取得（プライベートリポジトリ用）

        環境変数 GITHUB_TOKEN が最優先。設定ファイル側の値は
        機器パスワードと同じく DPAPI で暗号化して保存する。
        """
        # 環境変数を優先
        import os
        env_token = os.environ.get('GITHUB_TOKEN')
        if env_token:
            return env_token
        
        # 設定ファイルから取得
        token = self.get_update_settings().get("github_token")
        if not isinstance(token, str) or not token:
            return None
        if self.crypto.is_encrypted(token):
            # 別の Windows アカウント/PC で保存された設定。暗号文を
            # Authorization ヘッダへ載せても 401 になるだけで、
            # 利用者には原因が分からない
            print("[Config] GitHubトークンを復号できませんでした。"
                  "別の Windows アカウント/PC で保存された設定の可能性があります。"
                  "トークンは再設定してください。")
            return None
        return token
    
    def set_github_token(self, token: Optional[str]) -> bool:
        """GitHubトークンを設定"""
        return self.update_update_settings({"github_token": token})
    
    def add_device(self, group_name: str, device_info: Dict) -> bool:
        """
        機器を追加
        
        Args:
            group_name: 所属グループ名
            device_info: 機器情報
            
        Returns:
            追加成功時True、失敗時False
        """
        group = self.get_group(group_name)
        if not group:
            return False
        # 機器名は全グループを通して一意。接続の管理も所属グループの検索も
        # 名前だけで行うので、別グループに同名があると、先に見つかった方の
        # auto_commands が送られる
        if self.find_device_group(device_info.get("name", "")) is not None:
            return False
        if is_reserved_device_name(device_info.get("name", "")):
            return False

        before = list(group["devices"])
        group["devices"].append(device_info)
        if self.save_config():
            # 同じ名前を作り直した場合に、前の機器の「復号できない」記録を
            # 引きずらない
            self._refresh_undecryptable(device_info.get("name", ""), device_info)
            return True
        group["devices"][:] = before   # 保存できなかったらメモリも戻す
        return False

    def remove_device(self, group_name: str, device_name: str,
                      endpoint=None) -> bool:
        """機器を 1 件だけ削除（対象の機器が無いときも False）

        名前で絞ると、同じグループに同名が 2 台あるときに 2 台とも消える。
        画面の確認も完了も 1 台の話をするので、消えたことが伝わらない
        （remove_group は先頭の 1 件だけを消す形に揃っている）。
        endpoint には削除したい機器の接続先（device_endpoint() の戻り値）を
        渡す。省略すると先頭の 1 件を消す。
        """
        group = self.get_group(group_name)
        if not group:
            return False

        before = list(group["devices"])
        index = self._device_index(before, device_name, endpoint)
        if index is None:
            # 何も消していないので保存もしない。保存結果の True を返すと、
            # 呼び出し側が「削除しました」と案内してしまう（remove_group と同じ）
            return False
        group["devices"] = before[:index] + before[index + 1:]
        if self.save_config():
            self.undecryptable_devices.pop(device_name, None)
            return True
        # 保存できなかったのにメモリから消したままだと、次の無関係な保存で
        # 機器がディスクから消える
        group["devices"] = before
        return False

    @staticmethod
    def _device_index(devices: List[Dict], device_name: str,
                      endpoint=None) -> Optional[int]:
        """その名前の機器が一覧のどこにいるかを返す（無ければ None）。

        機器名は本来グループをまたいで一意だが、手編集や他ツールの
        config.json では同じグループに同名が並ぶ。名前だけで探すと必ず
        先頭に当たるので、2 台目を編集・削除したつもりで 1 台目が
        書き換わったり消えたりする（実測）。呼び出し側が操作対象の
        接続先を知っているときは、それで 1 件に絞る。
        接続先でも絞れない（完全に同じ機器が 2 つある）ときは先頭。
        """
        found = [i for i, d in enumerate(devices)
                 if isinstance(d, dict) and d.get("name") == device_name]
        if not found:
            return None
        if len(found) > 1 and endpoint is not None:
            matched = [i for i in found if device_endpoint(devices[i]) == endpoint]
            if len(matched) == 1:
                return matched[0]
        return found[0]

    def find_device_group(self, device_name: str) -> Optional[str]:
        """その名前の機器が属するグループ名を返す（無ければ None）"""
        for group in self.get_groups():
            for device in group.get("devices", []):
                if device.get("name") == device_name:
                    return group["name"]
        return None

    def update_device(self, group_name: str, old_name: str,
                      new_group_name: str, device_info: Dict,
                      old_endpoint=None) -> bool:
        """機器を差し替える（改名・グループ移動を含む）。保存は 1 回。

        remove_device → add_device の 2 段階にすると、間の状態がディスクに
        残ったり、片方の保存だけ失敗して機器が消えたり新旧 2 件になったり
        する。差し替えをメモリ上で組んでから 1 回だけ保存し、失敗したら
        メモリも元に戻す。

        old_endpoint には編集前の機器の接続先（device_endpoint() の戻り値）を
        渡す。同じグループに同名が並んでいるときに、どちらを編集したのかは
        名前だけでは決まらないため。省略すると先頭の 1 件を編集する。
        """
        source = self.get_group(group_name)
        target = self.get_group(new_group_name)
        if not source or not target:
            return False
        new_name = device_info.get("name", "")
        if is_reserved_device_name(new_name):
            return False
        # 名前を変えないなら、その名前は編集している当の機器のものなので衝突
        # ではない。find_device_group() は全グループを通して先頭の 1 件しか
        # 返さないため、同じ名前の機器が 2 グループにあると、2 台目は
        # owner が別グループになって「別の機器の名前」と判定され、パスワード
        # を入れ直すだけの保存まで断られていた（復号できなかったときの
        # 「機器の編集で入れ直してください」が、その機器だけ実行できない）。
        # 改名するときだけ、その名前が既に使われていないかを見る。
        if new_name != old_name and self.find_device_group(new_name) is not None:
            return False   # 別の機器の名前
        index = self._device_index(source["devices"], old_name, old_endpoint)
        if index is None:
            return False
        # グループを移すなら、移動先に同名が居ないかを見る。名前を変えない
        # 編集は上の重複検査を通らないので、ここを見ないとドラッグ＆ドロップ
        # （move_device）が断る移動を、編集のグループ欄からは黙って通せる
        if source is not target and any(
                d.get("name") == new_name for d in target["devices"]):
            print(f"エラー: デバイス '{new_name}' は移動先グループに既に存在します")
            return False

        source_before = list(source["devices"])
        target_before = list(target["devices"])
        if source is target:
            source["devices"][index] = device_info
        else:
            del source["devices"][index]
            target["devices"].append(device_info)
        if self.save_config():
            self._refresh_undecryptable(old_name, device_info)
            return True
        source["devices"][:] = source_before
        target["devices"][:] = target_before
        return False
    
    def get_global_macros(self) -> List[Dict]:
        """全体共通マクロ一覧を取得"""
        return self.config.get("global_macros", [])
    
    def add_global_macro(self, macro_name: str, commands: List[str], description: str = "") -> bool:
        """
        全体共通マクロを追加
        
        Args:
            macro_name: マクロ名
            commands: コマンドリスト
            description: マクロの説明
            
        Returns:
            追加成功時True、失敗時False
        """
        # 同名マクロが存在しないかチェック
        if self.get_macro_by_name(macro_name):
            return False
        
        new_macro = {
            "name": macro_name,
            "commands": commands,
            "description": description
        }
        
        if "global_macros" not in self.config:
            self.config["global_macros"] = []
        
        macros = self.config["global_macros"]
        macros.append(new_macro)
        if self.save_config():
            return True
        # 保存できなかったのにメモリへ残すと、次の無関係な保存で
        # 追加できなかったはずのコマンド列がディスクに確定する
        macros.pop()
        return False
    
    def get_macro_by_name(self, macro_name: str) -> Optional[Dict]:
        """
        指定したマクロを取得
        
        Args:
            macro_name: マクロ名
            
        Returns:
            マクロデータ、見つからない場合None
        """
        for macro in self.config.get("global_macros", []):
            if macro.get("name") == macro_name:
                return macro
        return None
    
    def update_global_macro(self, macro_name: str, commands: List[str], description: str = "") -> bool:
        """
        全体共通マクロを更新
        
        Args:
            macro_name: マクロ名
            commands: コマンドリスト
            description: マクロの説明
            
        Returns:
            更新成功時True、失敗時False
        """
        macro = self.get_macro_by_name(macro_name)
        if not macro:
            return False
        
        before = dict(macro)
        macro["commands"] = commands
        macro["description"] = description
        if self.save_config():
            return True
        # 保存できなかった編集を残さない（add_device と同じ）
        macro.clear()
        macro.update(before)
        return False
    
    def remove_global_macro(self, macro_name: str) -> bool:
        """
        全体共通マクロを削除
        
        Args:
            macro_name: マクロ名
            
        Returns:
            削除成功時True、失敗時False（対象のマクロが無いときも False）
        """
        if "global_macros" not in self.config:
            return False
        
        macros = self.config["global_macros"]
        before = list(macros)
        remaining = [m for m in before if m.get("name") != macro_name]
        if len(remaining) == len(before):
            # 何も消していないので保存もしない。保存結果の True を返すと、
            # 呼び出し側が「削除しました」と案内してしまう（remove_group と同じ）
            return False
        self.config["global_macros"] = remaining
        if self.save_config():
            return True
        # 保存できなかったのにメモリから消すと、次の無関係な保存で
        # 消せなかったはずのマクロがディスクから消える
        self.config["global_macros"] = before
        return False
    
    def get_settings(self) -> Dict:
        """アプリケーション設定を取得"""
        return self.config.get("settings", {})
    
    def update_settings(self, settings: Dict) -> bool:
        """アプリケーション設定を更新"""
        self.config["settings"] = settings
        return self.save_config()
    
    def _settings_section(self, key, create=False):
        """
        settings.<key> を dict として返す

        config.json は手で編集できるため、settings 自体や各セクションが
        dict でなくなっていることがある。そのまま .update() すると
        AttributeError で落ちるので、ここで型を保証する。

        Args:
            key: セクション名
            create: 壊れていた場合に config を作り直すか（書き込み時は True）

        Returns:
            セクションの dict。create=False で壊れていれば空 dict
        """
        settings = self.config.get("settings")
        if not isinstance(settings, dict):
            if not create:
                return {}
            settings = {}
            self.config["settings"] = settings

        section = settings.get(key)
        if not isinstance(section, dict):
            if not create:
                return {}
            section = {}
            settings[key] = section

        return section
    
    def get_last_save_dir(self):
        """前回保存したフォルダを返す（覚えていなければ None）

        保存ダイアログを前回と同じ場所から開くためだけの値。覚えるのは
        フォルダのパス 1 つで、ファイル名も機器の情報も入れない。
        config.json は手で編集できるので、文字列以外が入っていたら
        覚えていない扱いにする（os.path.isdir へ渡して落とさない）。
        """
        value = self._settings_section(_LAST_SAVE_DIR_SECTION).get(
            _LAST_SAVE_DIR_KEY)
        if isinstance(value, str) and value:
            return value
        return None

    def set_last_save_dir(self, directory) -> bool:
        """前回保存したフォルダを覚える"""
        self._settings_section(_LAST_SAVE_DIR_SECTION,
                               create=True)[_LAST_SAVE_DIR_KEY] = directory
        return self.save_config()

    def get_server_settings(self, key):
        """サーバー設定 dict を返す（key='tftp_server'/'ftp_server'/'sftp_server'）。"""
        return self._settings_section(key)
    
    def set_server_settings(self, key, values):
        """サーバー設定をマージして保存する。"""
        self._settings_section(key, create=True).update(values)
        return self.save_config()
    
    def move_device(self, source_group_name: str, target_group_name: str,
                    device_name: str, endpoint=None) -> bool:
        """
        デバイスをグループ間で移動

        Args:
            source_group_name: 移動元グループ名
            target_group_name: 移動先グループ名
            device_name: デバイス名
            endpoint: 掴んだ機器の接続先（device_endpoint() の戻り値）。
                同じグループに同名が並んでいるとき、どの 1 台を動かすかを
                これで決める。省略時は今までどおり先頭の 1 件

        Returns:
            移動成功時True、失敗時False
        """
        # 移動元グループを取得
        source_group = self.get_group(source_group_name)
        if not source_group:
            print(f"エラー: 移動元グループ '{source_group_name}' が見つかりません")
            return False
        
        # 移動先グループを取得
        target_group = self.get_group(target_group_name)
        if not target_group:
            print(f"エラー: 移動先グループ '{target_group_name}' が見つかりません")
            return False
        
        # 移動対象デバイスを検索。名前だけで探すと同名の先頭に当たるので、
        # 掴んだ項目の接続先で 1 台に絞る（編集・削除と同じ）
        index = self._device_index(source_group["devices"], device_name, endpoint)
        if index is None:
            print(f"エラー: デバイス '{device_name}' が移動元グループに見つかりません")
            return False
        device_to_move = source_group["devices"][index]

        # 移動先グループに同名デバイスがないかチェック
        for device in target_group["devices"]:
            if device["name"] == device_name:
                print(f"エラー: デバイス '{device_name}' は移動先グループに既に存在します")
                return False
        
        # デバイスを移動元から削除
        # 保存に失敗したら戻せるよう、両方の一覧を控える（update_device と同じ）
        source_before = list(source_group["devices"])
        target_before = list(target_group["devices"])
        # remove() は同じ中身の辞書が 2 つあると先頭を消すので、位置で消す
        del source_group["devices"][index]

        # デバイスを移動先に追加
        target_group["devices"].append(device_to_move)
        
        # 設定を保存
        result = self.save_config()
        if not result:
            # 保存できなかったのに移動したままだと、次の無関係な保存で
            # ディスク側だけが移動した状態になる
            source_group["devices"][:] = source_before
            target_group["devices"][:] = target_before
        if result:
            print(f"[INFO] デバイス '{device_name}' を '{source_group_name}' から '{target_group_name}' に移動しました")
        
        return result