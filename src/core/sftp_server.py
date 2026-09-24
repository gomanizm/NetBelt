"""SFTP サーバー実装"""
import os
import socket
import threading
import time
import paramiko
from paramiko import ServerInterface, SFTPServerInterface, SFTPServer, SFTPAttributes, SFTPHandle, SFTP_OK, SFTP_FAILURE
from PyQt6.QtCore import QObject, pyqtSignal
import stat as stat_module
from .sockets import set_exclusive_bind
from .crypto import PasswordCrypto
from .ftp_server import UNDECRYPTABLE_PASSWORD_MESSAGE
# 停止のあとも書き込みが生き残っている間に、次の起動を断る理由の文言。
# 生き残りは保存先のファイルを握ったままなので、TFTP と同じ扱いにする
from .tftp_server import PREVIOUS_STOP_INCOMPLETE_MESSAGE


class _OpenWriters:
    """書き込み用に開いているハンドルと、それを扱う SFTP のスレッドの一覧。

    停止のあとも保存先を握ったまま生き残ったスレッドを見分けるのに使う。
    ハンドルは SFTP のスレッドが登録・解除し、マネージャが読む
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}   # ハンドル -> (スレッド, 実パス)
        # 書き込みで開いている保存先の鍵 -> 開いたスレッド（reserve を参照）
        self._reserved = {}

    @staticmethod
    def _key(real_path):
        """保存先を比べるための鍵（TFTPServer._target_key と同じ考え方）。

        実パスは realpath 済み。Windows は大文字小文字を区別しないので
        normcase で揃える（区切り文字も '\\' へ揃う）
        """
        return os.path.normcase(real_path)

    def reserve(self, real_path):
        """保存先を 1 本の書き込みに予約する。先客がいれば False。

        開く前に取る。同じ保存先を 2 本が同時に書くと、後から開いた側の
        O_TRUNC と先の側の書き込みが交互に効いて、どちらも成功で終わるのに
        中身だけが混ざる。閉じないまま終わったスレッドの予約は先客に数えない
        """
        key = self._key(real_path)
        with self._lock:
            holder = self._reserved.get(key)
            if holder is not None and holder.is_alive():
                return False
            self._reserved[key] = threading.current_thread()
            return True

    def release(self, real_path):
        """開けなかった保存先の予約を外す"""
        with self._lock:
            self._reserved.pop(self._key(real_path), None)

    def held_by_other(self, real_path):
        """別の生きているスレッド（別の接続）が保存先を予約しているか。

        予約はしない。SETSTAT の切り詰めを断るかの判定に使う（書いている
        本人の切り詰めは通す）
        """
        with self._lock:
            holder = self._reserved.get(self._key(real_path))
            return (holder is not None and holder.is_alive()
                    and holder is not threading.current_thread())

    def add(self, handle, real_path):
        with self._lock:
            self._entries[handle] = (threading.current_thread(), real_path)

    def discard(self, handle):
        with self._lock:
            entry = self._entries.pop(handle, None)
            # 閉じ終えたので予約も外す（自分が取った予約だけ）
            if entry is not None and \
                    self._reserved.get(self._key(entry[1])) is entry[0]:
                del self._reserved[self._key(entry[1])]

    def alive(self):
        """生きているスレッドが開いている分を (スレッド, 実パス) で返す。

        閉じないまま終わったスレッドの分はここで落とす
        """
        with self._lock:
            for handle, (thread, _path) in list(self._entries.items()):
                if not thread.is_alive():
                    del self._entries[handle]
            return list(self._entries.values())


class _WriteHandle(SFTPHandle):
    """書き込み用のハンドル。閉じ終えたら _OpenWriters から外れる。

    close() の中で止まっている間（共有フォルダへの書き出しなど）は
    一覧に残るので、まだ保存先を握っていると分かる
    """

    def __init__(self, flags, open_writers):
        super().__init__(flags)
        self._open_writers = open_writers

    def close(self):
        try:
            super().close()
        finally:
            self._open_writers.discard(self)


class SFTPServerHandler(SFTPServerInterface):
    """SFTP サーバーハンドラー"""
    
    def __init__(self, server, root_dir, *args, open_writers=None,
                 notify=None, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.root_dir = os.path.abspath(root_dir)
        # 書き込み用に開いたハンドルを登録する先（SFTPServerManager の一覧）
        self._open_writers = open_writers
        # 利用者へ見せる出来事をパネルのログへ渡す口（1 行の文字列を受け取る）
        self._notify = notify
        
    def _get_real_path(self, path):
        """SFTP のパスを実際のファイルシステムパスに変換する（chroot を模擬）。

        SFTP のパス区切りは常に '/'（RFC）。一方 Windows の os.path.normpath は
        '/' を '\\' に変換するため、素直に normpath してから lstrip('/') すると
        区切りを剥がせず、os.path.join(root_dir, '\\foo') がベースを破棄して
        'C:\\foo' になる。そのため先に区切りを '/' へ統一し、先頭の '/' を
        落として必ず root_dir 配下へ寄せる。

        判定には realpath を使う。abspath は '..' を畳むだけでシンボリックリンクや
        ジャンクションを解決しないため、root 配下にリンクを1つ置かれるだけで
        その先の任意の場所へ到達できてしまう（Windows のジャンクションは
        一般ユーザーでも作成できる）。
        """
        relative = path.replace("\\", "/").lstrip("/")
        root_real = os.path.realpath(self.root_dir)
        real_path = os.path.realpath(os.path.join(root_real, relative))

        # セキュリティチェック: ルートディレクトリ外へのアクセスを防ぐ。
        # 接頭辞は join(root, '') で作る。root がドライブ直下（'D:\\'）だと
        # realpath が区切りで終わるので、単純に os.sep を足すと 'D:\\\\' に
        # なり、直下のあらゆるパスが外側と判定されてしまう。
        if real_path != root_real and not real_path.startswith(os.path.join(root_real, "")):
            raise IOError("Access denied")

        return real_path

    def _get_link_path(self, path):
        """削除・改名の対象パスを返す（最終要素はリンクを解決しない）。

        _get_real_path は最終要素まで realpath で解決するので、ルート内の
        alias → target というリンクに対して「alias を消す」と target 自体を
        消してしまう。閉じ込めの判定は親ディレクトリを解決して行い、
        最終要素はその名前のまま扱う。リンクを通ってルートの外へ出る
        パス（escape/secret.txt）は親が外側に解決されるので、これまで
        どおり拒否される。
        """
        relative = path.replace("\\", "/").strip("/")
        if not relative:
            raise IOError("Access denied")
        parent_rel, _, leaf = relative.rpartition("/")
        if leaf in ("", ".", ".."):
            raise IOError("Access denied")
        # ドライブ指定の付いた leaf を拒否する。Windows の os.path.join は
        # 'C:name' のような「ドライブ相対」を渡されると左側を丸ごと捨てるため、
        # 公開ルートと別ドライブを指す名前ひとつで閉じ込めが外れる
        # （そのドライブのカレントディレクトリ直下＝アプリの起動場所に届く）。
        # コロンを含む名前は Windows では作成できないので、正規の要求では起きない。
        if os.path.splitdrive(leaf)[0] or os.path.isabs(leaf):
            raise IOError("Access denied")
        parent_real = self._get_real_path(parent_rel)
        return os.path.join(parent_real, leaf)

    def list_folder(self, path):
        """ディレクトリ一覧を返す。

        各項目は os.lstat で引く（SFTP の READDIR は lstat 相当を返すのが慣例）。
        os.stat だとリンクを解決してしまうため、(1) リンクはリンク先の属性で
        報告され、同じ名前に対する LSTAT の答えと食い違う、(2) リンク先を失った
        リンクは stat が失敗して一覧から丸ごと消える（ディスク上には実在し、
        名前を指定すれば削除できるのに見えない）という2つの食い違いが起きる。

        残る制限: Windows のジャンクションは os.lstat でも S_IFLNK が立たず
        （実測で st_mode = 0o40777）、一覧の側からリンクだと見分けられない。
        開発者モード等で作れる本来のシンボリックリンクでは S_IFLNK が立つ。

        それでも引けなかった項目だけを落とす。握りつぶすと「一覧に出ない」
        理由が誰にも分からなくなるので、名前と理由をログに出す。
        """
        try:
            real_path = self._get_real_path(path)

            if not os.path.isdir(real_path):
                return SFTP_FAILURE

            items = []
            for filename in os.listdir(real_path):
                file_path = os.path.join(real_path, filename)
                try:
                    stat_info = os.lstat(file_path)
                    attr = SFTPAttributes.from_stat(stat_info, filename)
                    items.append(attr)
                except Exception as e:
                    print(f"[SFTP Server] list_folder skipped {filename}: {e}")
                    continue

            return items
        except Exception as e:
            print(f"[SFTP Server] list_folder error: {e}")
            return SFTP_FAILURE
    
    def stat(self, path):
        """ファイル/ディレクトリの情報を返す"""
        try:
            real_path = self._get_real_path(path)
            stat_info = os.stat(real_path)
            return SFTPAttributes.from_stat(stat_info)
        except Exception as e:
            print(f"[SFTP Server] stat error: {e}")
            return SFTP_FAILURE
    
    def lstat(self, path):
        """ファイル/ディレクトリの情報を返す（シンボリックリンクをたどらない）。

        最終要素まで realpath で解決してから os.lstat を呼ぶと、LSTAT が STAT と
        同じ意味になり、リンク自身ではなくリンク先の属性を返してしまう
        （os.lstat 自体はリンクを解決しないので、渡す前に解決したら取り返せない）。
        閉じ込めの判定は親ディレクトリを解決して行い、最終要素はその名前のまま
        os.lstat へ渡す。ルート自身と '.' / '..' はリンクになり得ないので従来どおり
        解決する。リンクを通ってルートの外へ出るパスは、親が外側に解決されるので
        これまでどおり拒否される。
        """
        try:
            relative = path.replace("\\", "/").strip("/")
            if not relative or relative.rpartition("/")[2] in ("", ".", ".."):
                real_path = self._get_real_path(path)
            else:
                real_path = self._get_link_path(path)
            stat_info = os.lstat(real_path)
            return SFTPAttributes.from_stat(stat_info)
        except Exception as e:
            print(f"[SFTP Server] lstat error: {e}")
            return SFTP_FAILURE
    
    def open(self, path, flags, attr):
        """ファイルを開く。

        SFTP のフラグ（O_TRUNC / O_CREAT / O_EXCL / O_APPEND）をそのまま OS へ渡す。
        書き込み系を一律 'wb' で開くと、読み書き両用で開いた瞬間に既存ファイルが
        truncate され、部分書き換えを行うクライアントがデータを失う。
        O_EXCL（新規作成、存在したら失敗）も黙って上書きしてしまう。

        同じ保存先を別の要求が書き込みで開いている間は、書き込みの open を
        断る（_OpenWriters.reserve を参照）。O_TRUNC を伴う open も同じ扱いに
        する（WRITE を立てない READ|CREATE|TRUNC でも、Windows の os.open() は
        相手の書きかけを 0 バイトに切り詰める）。それ以外の読み取りの open は
        妨げない。
        """
        reserved = False
        try:
            real_path = self._get_real_path(path)

            writing = bool(flags & (os.O_WRONLY | os.O_RDWR))

            # 親ディレクトリの用意は書き込み時のみ。
            # 読み取り目的の open で空ディレクトリを作らないため。
            if writing:
                dir_path = os.path.dirname(real_path)
                if dir_path and not os.path.exists(dir_path):
                    os.makedirs(dir_path)

            # 予約するのは書き込みか切り詰めを伴う open。モードとハンドルの
            # 選び方は writing のまま（切り詰めだけの open は読み取り専用）
            tracked = ((writing or bool(flags & os.O_TRUNC))
                       and self._open_writers is not None)
            if tracked and not self._open_writers.reserve(real_path):
                # 開くと O_TRUNC が相手の書きかけを切り詰め、双方が成功で
                # 終わるのに中身が混ざる。TFTP の同名 WRQ と同じく断る
                print(f"[SFTP Server] open refused, already open for writing: {real_path}")
                if self._notify is not None:
                    self._notify("他の転送が書き込み中のため断りました: %s" % path)
                return SFTP_FAILURE
            reserved = tracked

            fd = os.open(real_path, flags | getattr(os, "O_BINARY", 0))
            if flags & os.O_RDWR:
                mode = 'a+b' if (flags & os.O_APPEND) else 'r+b'
            elif writing:
                mode = 'ab' if (flags & os.O_APPEND) else 'wb'
            else:
                mode = 'rb'
            f = os.fdopen(fd, mode)

            fobj = (_WriteHandle(flags, self._open_writers) if tracked
                    else SFTPHandle(flags))
            # モードに応じて片方だけ設定する。両方入れると、読み取り専用の
            # ハンドルが書き込み可能として応答してしまう。
            if flags & os.O_RDWR:
                fobj.readfile = f
                fobj.writefile = f
            elif writing:
                fobj.writefile = f
            else:
                fobj.readfile = f
            fobj._real_path = real_path
            if tracked:
                # 閉じ終えるまで、このスレッドが保存先を握っていると覚える
                self._open_writers.add(fobj, real_path)

            return fobj
        except Exception as e:
            if reserved:
                # 開けなかった。予約を残すと、その保存先へ二度と書けなくなる
                self._open_writers.release(real_path)
            print(f"[SFTP Server] open error: {e}")
            return SFTP_FAILURE

    def remove(self, path):
        """ファイルを削除"""
        try:
            # リンクの先ではなくリンク自体を消す
            os.remove(self._get_link_path(path))
            return SFTP_OK
        except Exception as e:
            print(f"[SFTP Server] remove error: {e}")
            return SFTP_FAILURE
    
    def rename(self, oldpath, newpath):
        """ファイル/ディレクトリ名を変更"""
        try:
            # リンクの先ではなくリンク自体を改名する
            os.rename(self._get_link_path(oldpath), self._get_link_path(newpath))
            return SFTP_OK
        except Exception as e:
            print(f"[SFTP Server] rename error: {e}")
            return SFTP_FAILURE
    
    def mkdir(self, path, attr):
        """ディレクトリを作成"""
        try:
            real_path = self._get_real_path(path)
            os.mkdir(real_path)
            return SFTP_OK
        except Exception as e:
            print(f"[SFTP Server] mkdir error: {e}")
            return SFTP_FAILURE
    
    def rmdir(self, path):
        """ディレクトリを削除"""
        try:
            # リンクの先ではなくリンク自体を外す（ジャンクションは rmdir で外れる）
            os.rmdir(self._get_link_path(path))
            return SFTP_OK
        except Exception as e:
            print(f"[SFTP Server] rmdir error: {e}")
            return SFTP_FAILURE
    
    def chattr(self, path, attr):
        """ファイル属性を変更（SETSTAT）。

        サイズ・日時・パーミッションを要求どおり反映する。以前は st_mode
        だけを見て、サイズと日時は捨てたまま SFTP_OK を返していたため、
        truncate や日時保持（sftp -p）がエラーも出ないまま効かなかった。

        制限: uid/gid は Windows で意味を持たないので受け取っても無視する
        （この用途の SFTP クライアントは所有者を送ってこない）。開いている
        ハンドルへの FSETSTAT は paramiko の既定のまま「未対応」を返す。
        """
        try:
            real_path = self._get_real_path(path)
            if attr.st_size is not None:
                # 別の接続が書き込み中の保存先は切り詰めない。相手は成功で
                # 終わるのに、切り詰めた先頭が 0 で埋まって中身が壊れる
                if self._open_writers is not None and \
                        self._open_writers.held_by_other(real_path):
                    print(f"[SFTP Server] truncate refused, open for writing: {real_path}")
                    if self._notify is not None:
                        self._notify("他の転送が書き込み中のため断りました: %s" % path)
                    return SFTP_FAILURE
                os.truncate(real_path, attr.st_size)
            atime = attr.st_atime
            mtime = attr.st_mtime
            if atime is not None or mtime is not None:
                # 片方だけ指定されたら、もう片方は現在の値を保つ
                current = os.stat(real_path)
                os.utime(real_path,
                         (current.st_atime if atime is None else atime,
                          current.st_mtime if mtime is None else mtime))
            # 読み取り専用にする要求が先に効くと、同じ要求内の truncate や
            # utime が通らなくなるので、モードは最後に適用する
            if attr.st_mode is not None:
                os.chmod(real_path, attr.st_mode)
            return SFTP_OK
        except Exception as e:
            print(f"[SFTP Server] chattr error: {e}")
            return SFTP_FAILURE


class SSHServerInterface(ServerInterface):
    """SSH サーバーインターフェース"""
    
    def __init__(self, username, password):
        super().__init__()
        self.username = username
        self.password = password
    
    def check_auth_password(self, username, password):
        """パスワード認証"""
        if username == self.username and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED
    
    def check_channel_request(self, kind, chanid):
        """チャネルリクエストの許可"""
        if kind == 'session':
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
    
    def get_allowed_auths(self, username):
        """許可する認証方法"""
        return 'password'


class SFTPServerManager(QObject):
    """SFTP サーバーマネージャー"""
    
    # シグナル定義
    started = pyqtSignal()
    stopped = pyqtSignal()
    client_connected = pyqtSignal(str)  # クライアントIP
    client_disconnected = pyqtSignal(str)  # クライアントIP
    client_activity = pyqtSignal(str, str)  # ip, message
    file_uploaded = pyqtSignal(str, str)  # クライアントIP, ファイル名
    file_downloaded = pyqtSignal(str, str)  # クライアントIP, ファイル名
    error_occurred = pyqtSignal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.is_running = False
        self.server_socket = None
        self.server_thread = None
        self.client_threads = []
        # 受け付けたクライアントのソケット。stop() で閉じて、バナー待ちや
        # チャネル待ちで止まっているハンドラをその場で抜けさせる
        self._client_sockets = set()
        self._client_lock = threading.Lock()
        self._stop_event = threading.Event()
        # 書き込み用に開いているハンドルの一覧（SFTP のスレッドが登録する）と、
        # 停止のあとも書き込みを抱えたまま生き残ったスレッド
        self._open_writers = _OpenWriters()
        self._unfinished = []

        # 同時に受け付けるクライアント接続の上限。認証前の接続でも
        # スレッドと Transport を 1 つずつ消費するので、上限が無いと
        # 待受アドレス（0.0.0.0）へ届く任意のホストに資源を積まれる。
        # 他サーバと桁を揃える（TFTP は 16、Syslog TCP は 64）
        self.max_client_connections = 32
        # 認証前の接続を打ち切る期限（秒）。paramiko の既定値と同じだが、
        # 既定に頼らず明示して、枠が返る時間を上限側で決められるようにする
        self.banner_timeout_seconds = 15.0
        self.auth_timeout_seconds = 30.0
        # 接続がこの秒数黙ったら keepalive を送る。回線断などで相手が黙って
        # 消えると、TCP は送るものが無い限り気づけず、書き込みの予約
        # （_OpenWriters）がサーバーを止めるまで残って同じ名前へのアップロードを
        # 断り続ける。送れば TCP の再送が尽きたところで接続が切れ、予約が外れる。
        # 生きている相手は読み捨てるだけなので、黙っている書き手の予約は外さない。
        # 認証の期限と同じ桁にし、FTP のデータ接続の無通信期限（300 秒）より短くする
        self.keepalive_seconds = 30.0

        # GUI へ渡したまま、まだ処理されていない通知の件数の上限。
        # 認証を通さない TCP 接続→即切断だけで接続と切断の両方が出るので、
        # GUI が他の処理で塞がっている間に Qt の配送キューへ際限なく積み上がる
        # （実測: 20 秒で 13130 件・約 11MB）。32 接続の上限は切断ごとに枠が
        # 戻るので累積を止めない。パネルのログの行数上限が効くのは配送の後。
        # FTP の max_pending_notices・TFTP の同名の仕掛けと同じく、超過中は
        # 数えるだけにして、はけた時点で省略した件数を 1 行だけ出す
        self.max_pending_notices = 1000
        self._notice_lock = threading.Lock()
        self._pending_notices = 0
        self._dropped_notices = 0
        # 接続を届けたかを接続ごとに覚える（_client_lock の下で読み書きする）。
        # 届けた接続の切断を省くと、パネルの「接続クライアント: N」が戻らない
        self._notice_shown = {}
        # 自分の信号を自分でも受ける。待受スレッド・ハンドラから emit した分は
        # キュー経由で GUI スレッドへ届くので、呼ばれたことが
        # 「GUI が 1 件処理した」の合図
        for signal in (self.client_connected, self.client_disconnected):
            signal.connect(self._on_notice_delivered)

        # サーバー設定
        self.port = 2222
        self.root_dir = "./sftp_root"
        self.username = ""
        self.password = ""
        
        # ホストキーは start() で読み込む（起動を待たせないため遅延）
        self.host_key = None
    
    def _take_notice(self, force=False):
        """配送待ちを 1 件ぶん確保する。確保できたら True（呼び出し側が emit する）。

        上限に達している間は False にして省略件数へ足す。force は上限を
        超えても確保する（接続を届けた相手の切断。捨てると接続数が戻らない）
        """
        with self._notice_lock:
            if not force and self._pending_notices >= self.max_pending_notices:
                self._dropped_notices += 1
                return False
            self._pending_notices += 1
            return True

    def _take_closing_notice(self, shown):
        """切断を渡すかを決める。

        接続を届けた相手（shown が True）の切断は必ず渡す。接続を省いた相手
        （False）のものは渡さずに省略件数へ足す。記録が無い（None）ものは
        上限の範囲でだけ渡す
        """
        if shown:
            return self._take_notice(force=True)
        if shown is False:
            with self._notice_lock:
                self._dropped_notices += 1
            return False
        return self._take_notice()

    def _on_notice_delivered(self, *_args):
        """GUI が通知を 1 件処理したので配送待ちを戻す（GUI スレッドで動く）"""
        with self._notice_lock:
            if self._pending_notices > 0:
                self._pending_notices -= 1
            dropped = 0
            if self._pending_notices == 0:
                dropped, self._dropped_notices = self._dropped_notices, 0
        if dropped:
            # この 1 行は数えない（GUI が追いついた時点でしか出ない）
            self.client_activity.emit(
                "", "表示が追いつかず %d 件の通知を省略しました" % dropped)

    def _load_or_create_host_key(self):
        """ホストキーをユーザデータディレクトリから読み込む。無ければ生成して保存する。

        起動のたびに生成すると毎回ホストキーが変わり、一度接続したクライアントは
        次回から host key mismatch で接続を拒否する（実質2回目以降使えない）。
        """
        from .config_manager import app_data_dir
        key_path = app_data_dir() / "sftp_host_key"
        if key_path.exists():
            try:
                return paramiko.RSAKey(filename=str(key_path))
            except Exception as e:
                print(f"[SFTP Server] ホストキーの読み込みに失敗したため再生成します: {e}")
        key = paramiko.RSAKey.generate(2048)
        try:
            key.write_private_key_file(str(key_path))
        except Exception as e:
            # 保存できなくても起動はできる（次回また変わる点だけ不利）
            print(f"[SFTP Server] ホストキーを保存できませんでした: {e}")
        return key

    def start(self, port: int = 2222, root_dir: str = "./sftp_root", 
              username: str = "", password: str = ""):
        """SFTPサーバーを起動"""
        if self.is_running:
            self.error_occurred.emit("サーバーは既に実行中です")
            return False
        # 前回の停止のあとも書き込みを抱えたスレッドが残っている間は起動しない。
        # 生き残りは保存先のファイルを握ったままなので、新しい起動で同名の
        # アップロードを受けると、後から戻った旧 write()/close() が中身を混ぜる。
        # ここでは待たない（画面を固めない）。消えていれば通す（TFTP と同じ）
        if not self._previous_stop_finished():
            self.error_occurred.emit(PREVIOUS_STOP_INCOMPLETE_MESSAGE)
            return False

        # 空の資格情報でネットワークに晒さない。UI 側でも検証しているが、
        # ここでも拒否して弱い既定値のまま起動する経路を残さない。
        if not username or not password:
            self.error_occurred.emit("ユーザー名とパスワードを指定してください")
            return False
        # 復号できなかった暗号文をそのまま認証パスワードにしない（FTP と同じ）
        if PasswordCrypto().is_encrypted(password):
            self.error_occurred.emit(UNDECRYPTABLE_PASSWORD_MESSAGE)
            return False
        
        self.port = port
        self.root_dir = os.path.abspath(root_dir)
        self.username = username
        self.password = password
        # ファイアウォールは自動設定しない（3CDaemon 方式）。管理者昇格(UAC)を避けるため、
        # 受信許可は Windows 標準の初回プロンプト／既存の許可ルールに委ねる。
        # 自動で足すと、ポートを変えて使うたびポート名入りのルールが恒久登録され、
        # 停止しても消えずに残骸が増える。通らない環境は fix_firewall() で直す。
        print("[SFTP Server] ファイアウォール: 自動設定なし（Windowsの許可に委ねます）")
        
        # ルートディレクトリが存在しない場合は作成
        if not os.path.exists(self.root_dir):
            try:
                os.makedirs(self.root_dir)
                print(f"[SFTP Server] Created root directory: {self.root_dir}")
            except Exception as e:
                self.error_occurred.emit(f"ルートディレクトリの作成に失敗: {str(e)}")
                return False
        
        # ホストキーを用意する（初回のみ生成、以後は使い回す）
        if self.host_key is None:
            self.host_key = self._load_or_create_host_key()

        # 先にバインドまで済ませ、失敗を戻り値で返す。
        # スレッドの中でバインドすると、ポート使用中でも start() が True を
        # 返してしまい、UI が「実行中」のまま何も待ち受けない状態になる。
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            set_exclusive_bind(sock)
            sock.bind(('0.0.0.0', self.port))
            sock.listen(5)
            sock.settimeout(1.0)  # 停止できるようタイムアウトを設ける
        except OSError as e:
            try:
                sock.close()
            except Exception:
                pass
            self.error_occurred.emit(
                f"ポート {self.port} で待ち受けできません: {e}")
            return False
        self.server_socket = sock

        # サーバースレッドを起動。停止フラグは起動ごとに作り直して渡す。
        # 使い回して clear() すると、前回の stop() で抜けきらなかった
        # 待受スレッドまで「停止していない」ことになり、新しいルート・
        # 資格情報で接続を処理してしまう（FTP の _serve と同じ形）
        self._stop_event = threading.Event()
        self.server_thread = threading.Thread(
            target=self._run_server, args=(sock, self._stop_event), daemon=True)
        self.server_thread.start()
        
        return True
    
    def fix_firewall(self, port: int = 2222):
        """手動: Windows FW 受信許可を追加（管理者昇格/UAC）

        起動時には触らない（TFTP/FTP と同じ 3CDaemon 方式）。Windows の
        初回プロンプトを拒否したなどで接続が通らない環境の復旧用で、
        押したときだけ昇格する。
        """
        try:
            from .firewall import (combine_results, ensure_inbound_allow,
                                   ensure_self_program_allow)
            ok, msg = ensure_inbound_allow("SFTP Server", "TCP", port)
            print(f"[SFTP Server] ファイアウォール: {msg}")
            ok2, msg2 = ensure_self_program_allow()
            print(f"[SFTP Server] ファイアウォール(自exe): {msg2}")
            return combine_results([(ok, msg), (ok2, msg2)])
        except Exception as e:
            print(f"[SFTP Server] ファイアウォール設定エラー: {e}")
            return False, str(e)

    # 待受スレッドの終了を待つ上限。accept は 1 秒でタイムアウトするので
    # 通常はそれ以内に抜ける
    STOP_TIMEOUT_SECONDS = 3.0
    # 停止のとき、書き込み中のスレッドが抜けるのを待つ上限（全体で）。
    # 切断に気づけば数ミリ秒で抜けるので、残るのは止まっている分だけ
    WRITER_STOP_TIMEOUT_SECONDS = 1.0

    def stop(self):
        """SFTPサーバーを停止する。

        書き込みを抱えたスレッドが残らず終われたかを返す（残っている間は
        次の start() を断る。_previous_stop_finished を参照）
        """
        thread = self.server_thread
        # is_running で判定しない。あのフラグを立てるのはワーカーの先頭で、
        # start() はスレッドを起こした直後に戻るため、start() の直後に
        # 呼ばれると「まだ立っていない」窓で空振りし、待受が生き残る
        if thread is None or not thread.is_alive():
            return self._previous_stop_finished()

        print("[SFTP Server] Stopping server...")
        # 停止フラグは接続一覧と同じロックの下で立てる。待受ループは
        # accept 復帰後に同じロックの下で「まだ停止していないか」を見て
        # から登録するので、どちらが先でも接続は必ずどちらかに閉じられる
        with self._client_lock:
            self._stop_event.set()
        self.is_running = False

        # サーバーソケットを閉じる
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass

        # クライアントのソケットを先に閉じる。ハンドラはバナー待ち
        # （start_server）やチャネル待ち（accept(timeout=20)）で止まって
        # いることがあり、join だけだと 1 本につき 1 秒固まったうえに
        # スレッドが残り、あとで client_disconnected を破棄済みの
        # マネージャへ emit して落ちる。閉じればどちらもすぐ抜ける。
        # ソケット一覧とスレッド一覧は同じロックの下で取る。待受ループが
        # 両方を 1 回のロックで登録するので、「_client_sockets に居る接続は
        # 必ず client_threads にも居る」が保たれる。join はロックの外で
        # 行う（ハンドラの後始末が同じロックを取るため）
        with self._client_lock:
            sockets = list(self._client_sockets)
            clients = list(self.client_threads)
            self.client_threads.clear()
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

        # クライアント接続の終了を待つ
        for client in clients:
            if client.is_alive():
                client.join(timeout=1)
        # 待受スレッド自身も待つ。待たずに戻ると、直後にこのマネージャ
        # （QObject）が破棄されたとき、まだ走っているスレッドからの emit が
        # 解放済みオブジェクトへ届く（FTP と同じ構造）
        if thread is not threading.current_thread():
            thread.join(timeout=self.STOP_TIMEOUT_SECONDS)
        # 接続はすべて閉じた。書き込み用のハンドルを開いたまま生きている
        # スレッドは、切断に気づいて抜ける途中か、保存先への write()/close()
        # の中で止まっている（共有フォルダの遅延・切断など）。前者を取り
        # 違えないよう少しだけ待ち、それでも残った分を覚えて、消えるまで
        # 次の start() を断る
        deadline = time.monotonic() + self.WRITER_STOP_TIMEOUT_SECONDS
        for writer, _path in self._open_writers.alive():
            writer.join(timeout=max(0.0, deadline - time.monotonic()))
        for writer, path in self._open_writers.alive():
            print(f"[SFTP Server] Write still in progress after stop: {path}")
            if writer not in self._unfinished:
                self._unfinished.append(writer)
        self.stopped.emit()
        print("[SFTP Server] Server stopped")
        return self._previous_stop_finished()

    def _previous_stop_finished(self):
        """前回の停止が終わっているか。今の状態だけを見て、待たない。

        書き込みを抱えて生き残ったスレッドのうち、終わった分は落とす
        """
        self._unfinished = [t for t in self._unfinished if t.is_alive()]
        return not self._unfinished
    
    def _run_server(self, sock, stop_event):
        """サーバーのメインループ

        sock と stop_event はこの起動のもの。停止待ちが期限切れになった後で
        起動し直されても、self の側（新しい起動）の停止フラグやソケットは
        見ない。
        """
        try:
            # ソケットは start() でバインド済み

            # start() の直後に stop() されていたら、ここで引き返す。
            # 進むと started が stopped の後に飛び、UI が「起動中」へ戻る
            if stop_event.is_set():
                return

            self.is_running = True
            self.started.emit()
            print(f"[SFTP Server] Server started on port {self.port}")
            print(f"[SFTP Server] Root directory: {self.root_dir}")
            print(f"[SFTP Server] Username: {self.username}")
            
            while not stop_event.is_set():
                try:
                    # accept は従来どおり self.server_socket に対して行う
                    # （テストが差し替えて accept の直後を止める）。ただし
                    # 停止フラグと同じロックの下で読む。stop() はこのロックの
                    # 下でフラグを立てるので、読めたのは必ずこの起動のソケットで、
                    # 起動し直した後の新しいソケットではない
                    with self._client_lock:
                        if stop_event.is_set():
                            break
                        listener = self.server_socket
                    # クライアント接続を待つ
                    client_socket, client_addr = listener.accept()
                    # 停止判定・ソケット登録・ハンドラの起動とスレッド一覧への
                    # 追加を、すべて同じロックの下で済ませる。どこかで一度でも
                    # ロックを離すと、その隙間に stop() が入った接続は誰にも
                    # 閉じられず join もされず、ハンドラが停止後に起きて
                    # 破棄済みかもしれないマネージャへ emit する
                    accepted = False
                    notice = False
                    reject_reason = ""
                    with self._client_lock:
                        if stop_event.is_set():
                            try:
                                client_socket.close()
                            except OSError:
                                pass
                            break
                        if len(self._client_sockets) >= self.max_client_connections:
                            # 上限に達している。枠が空くまでは受け付けず、
                            # スレッドも Transport も作らずにその場で閉じる
                            reject_reason = (
                                f"connection limit reached "
                                f"({self.max_client_connections})")
                        else:
                            self._client_sockets.add(client_socket)
                            client_thread = threading.Thread(
                                target=self._handle_client,
                                args=(client_socket, client_addr),
                                daemon=True
                            )
                            try:
                                client_thread.start()
                            except Exception as e:
                                # 起こせなかったハンドラのソケットは誰も
                                # 閉じない。登録を戻し、下で閉じる
                                self._client_sockets.discard(client_socket)
                                reject_reason = f"cannot start handler: {e}"
                            else:
                                # 終わったスレッドを外してから足す（Syslog と同じ）。
                                # 外さないとサーバを止めるまで単調に増える。stop() が
                                # 同じリストを走査するので、差し替えずその場で入れ替える
                                self.client_threads[:] = [t for t in self.client_threads
                                                          if t.is_alive()]
                                self.client_threads.append(client_thread)
                                # 配送枠はこのロックの下で決めて覚える。
                                # ハンドラの後始末も同じロックを取るので、
                                # 切断側がこの記録より先に読むことはない
                                notice = self._take_notice()
                                self._notice_shown[client_socket] = notice
                                accepted = True
                                if notice:
                                    # 接続の知らせもこのロックの下で出す。
                                    # 離してから出すと、その隙間に切れた相手の
                                    # 切断の知らせ（ハンドラの finally も同じ
                                    # ロックを取る）に追い越され、パネルの
                                    # 「接続クライアント: N」が 1 件ずれたまま
                                    # サーバを止めるまで戻らない
                                    self.client_connected.emit(client_addr[0])

                    if not accepted:
                        try:
                            client_socket.close()
                        except OSError:
                            pass
                        print(f"[SFTP Server] Rejected {client_addr[0]}:"
                              f"{client_addr[1]}: {reject_reason}")
                        continue

                    print(f"[SFTP Server] Client connected from {client_addr[0]}:{client_addr[1]}")

                except socket.timeout:
                    # タイムアウトは正常（停止チェックのため）
                    continue
                except Exception as e:
                    if stop_event.is_set():
                        break
                    print(f"[SFTP Server] Accept error: {e}")
                    
        except Exception as e:
            self.error_occurred.emit(f"サーバー起動エラー: {str(e)}")
            print(f"[SFTP Server] Server error: {e}")
        finally:
            # 後始末はこの起動の分だけ。起動し直されていたら is_running も
            # self.server_socket も新しい起動のものなので触らない
            if self._stop_event is stop_event:
                self.is_running = False
            try:
                sock.close()
            except OSError:
                pass
    
    def _handle_client(self, client_socket, client_addr):
        """クライアント接続を処理"""
        transport = None
        try:
            # SSHトランスポートを作成
            transport = paramiko.Transport(client_socket)
            # 認証前の接続に期限を持たせる。黙り込んだ相手が枠を占有し
            # 続けると、上限を入れても正規の接続が入れなくなる
            transport.banner_timeout = self.banner_timeout_seconds
            transport.auth_timeout = self.auth_timeout_seconds
            # 黙って消えた相手のセッションを終わらせる（keepalive_seconds を参照）
            transport.set_keepalive(self.keepalive_seconds)
            transport.add_server_key(self.host_key)
            
            # SFTP サブシステムを登録する。
            # paramiko の既定の ServerInterface.check_channel_subsystem_request は
            # ここで登録したハンドラを引いて起動する実装なので、登録しないと
            # クライアントの subsystem('sftp') 要求が拒否され、チャネルが閉じる。
            # 断った書き込みなどは、相手の IP を添えてパネルのログへ出す
            transport.set_subsystem_handler(
                'sftp', SFTPServer, SFTPServerHandler, root_dir=self.root_dir,
                open_writers=self._open_writers,
                notify=lambda message, ip=client_addr[0]:
                    self.client_activity.emit(ip, message))
            
            # SSHサーバーインターフェースを作成
            server = SSHServerInterface(self.username, self.password)
            
            # SSHネゴシエーション開始
            transport.start_server(server=server)
            
            # クライアントがチャネルを開くのを待つ
            channel = transport.accept(timeout=20)
            if channel is None:
                print(f"[SFTP Server] No channel from {client_addr[0]}")
                return
            
            # SFTPServer の生成と起動は set_subsystem_handler 経由で paramiko が行う。
            # ここで手動生成しても start() されないため、転送は始まらない。
            
            # チャネルが閉じるまで待つ
            while transport.is_active() and not self._stop_event.is_set():
                threading.Event().wait(0.5)
            
        except Exception as e:
            print(f"[SFTP Server] Client handler error: {e}")
        finally:
            if transport:
                transport.close()
            client_socket.close()
            with self._client_lock:
                self._client_sockets.discard(client_socket)
                shown = self._notice_shown.pop(client_socket, None)
            # 接続を届けた相手の切断は必ず届ける。省くとパネルの
            # 「接続クライアント: N」が減らないまま残る
            if self._take_closing_notice(shown):
                self.client_disconnected.emit(client_addr[0])
            print(f"[SFTP Server] Client disconnected from {client_addr[0]}")