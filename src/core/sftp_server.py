"""SFTP サーバー実装"""
import os
import socket
import threading
import paramiko
from paramiko import ServerInterface, SFTPServerInterface, SFTPServer, SFTPAttributes, SFTPHandle, SFTP_OK, SFTP_FAILURE
from PyQt6.QtCore import QObject, pyqtSignal
import stat as stat_module
from .sockets import set_exclusive_bind
from .crypto import PasswordCrypto
from .ftp_server import UNDECRYPTABLE_PASSWORD_MESSAGE


class SFTPServerHandler(SFTPServerInterface):
    """SFTP サーバーハンドラー"""
    
    def __init__(self, server, root_dir, *args, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.root_dir = os.path.abspath(root_dir)
        
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

        # セキュリティチェック: ルートディレクトリ外へのアクセスを防ぐ
        if real_path != root_real and not real_path.startswith(root_real + os.sep):
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
        parent_real = self._get_real_path(parent_rel)
        return os.path.join(parent_real, leaf)

    def list_folder(self, path):
        """ディレクトリ一覧を返す"""
        try:
            real_path = self._get_real_path(path)
            
            if not os.path.isdir(real_path):
                return SFTP_FAILURE
            
            items = []
            for filename in os.listdir(real_path):
                file_path = os.path.join(real_path, filename)
                try:
                    stat_info = os.stat(file_path)
                    attr = SFTPAttributes.from_stat(stat_info, filename)
                    items.append(attr)
                except Exception:
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
        """ファイル/ディレクトリの情報を返す（シンボリックリンクをたどらない）"""
        try:
            real_path = self._get_real_path(path)
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
        """
        try:
            real_path = self._get_real_path(path)

            writing = bool(flags & (os.O_WRONLY | os.O_RDWR))

            # 親ディレクトリの用意は書き込み時のみ。
            # 読み取り目的の open で空ディレクトリを作らないため。
            if writing:
                dir_path = os.path.dirname(real_path)
                if dir_path and not os.path.exists(dir_path):
                    os.makedirs(dir_path)

            fd = os.open(real_path, flags | getattr(os, "O_BINARY", 0))
            if flags & os.O_RDWR:
                mode = 'a+b' if (flags & os.O_APPEND) else 'r+b'
            elif writing:
                mode = 'ab' if (flags & os.O_APPEND) else 'wb'
            else:
                mode = 'rb'
            f = os.fdopen(fd, mode)

            fobj = SFTPHandle(flags)
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

            return fobj
        except Exception as e:
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
        """ファイル属性を変更"""
        try:
            real_path = self._get_real_path(path)
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
        
        # サーバー設定
        self.port = 2222
        self.root_dir = "./sftp_root"
        self.username = ""
        self.password = ""
        
        # ホストキーは start() で読み込む（起動を待たせないため遅延）
        self.host_key = None
    
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

        # サーバースレッドを起動
        self._stop_event.clear()
        self.server_thread = threading.Thread(target=self._run_server, daemon=True)
        self.server_thread.start()
        
        return True
    
    def fix_firewall(self, port: int = 2222):
        """手動: Windows FW 受信許可を追加（管理者昇格/UAC）

        起動時には触らない（TFTP/FTP と同じ 3CDaemon 方式）。Windows の
        初回プロンプトを拒否したなどで接続が通らない環境の復旧用で、
        押したときだけ昇格する。
        """
        try:
            from .firewall import ensure_inbound_allow, ensure_self_program_allow
            ok, msg = ensure_inbound_allow("SFTP Server", "TCP", port)
            print(f"[SFTP Server] ファイアウォール: {msg}")
            ok2, msg2 = ensure_self_program_allow()
            print(f"[SFTP Server] ファイアウォール(自exe): {msg2}")
            return (ok and ok2), msg
        except Exception as e:
            print(f"[SFTP Server] ファイアウォール設定エラー: {e}")
            return False, str(e)

    # 待受スレッドの終了を待つ上限。accept は 1 秒でタイムアウトするので
    # 通常はそれ以内に抜ける
    STOP_TIMEOUT_SECONDS = 3.0

    def stop(self):
        """SFTPサーバーを停止"""
        thread = self.server_thread
        # is_running で判定しない。あのフラグを立てるのはワーカーの先頭で、
        # start() はスレッドを起こした直後に戻るため、start() の直後に
        # 呼ばれると「まだ立っていない」窓で空振りし、待受が生き残る
        if thread is None or not thread.is_alive():
            return

        print("[SFTP Server] Stopping server...")
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
        # マネージャへ emit して落ちる。閉じればどちらもすぐ抜ける
        with self._client_lock:
            sockets = list(self._client_sockets)
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
        for client in self.client_threads:
            if client.is_alive():
                client.join(timeout=1)

        self.client_threads.clear()
        # 待受スレッド自身も待つ。待たずに戻ると、直後にこのマネージャ
        # （QObject）が破棄されたとき、まだ走っているスレッドからの emit が
        # 解放済みオブジェクトへ届く（FTP と同じ構造）
        if thread is not threading.current_thread():
            thread.join(timeout=self.STOP_TIMEOUT_SECONDS)
        self.stopped.emit()
        print("[SFTP Server] Server stopped")
    
    def _run_server(self):
        """サーバーのメインループ"""
        try:
            # ソケットは start() でバインド済み

            # start() の直後に stop() されていたら、ここで引き返す。
            # 進むと started が stopped の後に飛び、UI が「起動中」へ戻る
            if self._stop_event.is_set():
                return

            self.is_running = True
            self.started.emit()
            print(f"[SFTP Server] Server started on port {self.port}")
            print(f"[SFTP Server] Root directory: {self.root_dir}")
            print(f"[SFTP Server] Username: {self.username}")
            
            while not self._stop_event.is_set():
                try:
                    # クライアント接続を待つ
                    client_socket, client_addr = self.server_socket.accept()
                    with self._client_lock:
                        self._client_sockets.add(client_socket)

                    print(f"[SFTP Server] Client connected from {client_addr[0]}:{client_addr[1]}")
                    self.client_connected.emit(client_addr[0])
                    
                    # クライアントハンドラスレッドを起動
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client_socket, client_addr),
                        daemon=True
                    )
                    client_thread.start()
                    # 終わったスレッドを外してから足す（Syslog と同じ）。
                    # 外さないとサーバを止めるまで単調に増える。stop() が
                    # 同じリストを走査するので、差し替えずその場で入れ替える
                    self.client_threads[:] = [t for t in self.client_threads
                                              if t.is_alive()]
                    self.client_threads.append(client_thread)
                    
                except socket.timeout:
                    # タイムアウトは正常（停止チェックのため）
                    continue
                except Exception as e:
                    if self._stop_event.is_set():
                        break
                    print(f"[SFTP Server] Accept error: {e}")
                    
        except Exception as e:
            self.error_occurred.emit(f"サーバー起動エラー: {str(e)}")
            print(f"[SFTP Server] Server error: {e}")
        finally:
            self.is_running = False
            if self.server_socket:
                self.server_socket.close()
    
    def _handle_client(self, client_socket, client_addr):
        """クライアント接続を処理"""
        transport = None
        try:
            # SSHトランスポートを作成
            transport = paramiko.Transport(client_socket)
            transport.add_server_key(self.host_key)
            
            # SFTP サブシステムを登録する。
            # paramiko の既定の ServerInterface.check_channel_subsystem_request は
            # ここで登録したハンドラを引いて起動する実装なので、登録しないと
            # クライアントの subsystem('sftp') 要求が拒否され、チャネルが閉じる。
            transport.set_subsystem_handler(
                'sftp', SFTPServer, SFTPServerHandler, root_dir=self.root_dir)
            
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
            self.client_disconnected.emit(client_addr[0])
            print(f"[SFTP Server] Client disconnected from {client_addr[0]}")