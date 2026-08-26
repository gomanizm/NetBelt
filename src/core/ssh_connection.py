"""SSH接続管理"""
import paramiko
import threading
import time
from typing import Callable, Optional
from PyQt6.QtCore import QObject, pyqtSignal


class _TofuHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """未知ホストは受け入れて known_hosts に保存する(TOFU)。
    既知ホストで鍵が一致しない場合は paramiko が BadHostKeyException を送出する。
    """

    def __init__(self, known_hosts_path):
        self._known_hosts_path = known_hosts_path

    def missing_host_key(self, client, hostname, key):
        client.get_host_keys().add(hostname, key.get_name(), key)
        try:
            client.save_host_keys(str(self._known_hosts_path))
        except Exception:
            pass


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
        self._read_thread: Optional[threading.Thread] = None
        self._stop_reading = False
    
    def _setup_host_keys(self, client):
        """既知ホスト鍵を読み込み、TOFUポリシーを設定する。
        既知ホストで鍵が変わった場合は接続時に BadHostKeyException となる。
        """
        from .config_manager import app_data_dir
        known_hosts_path = app_data_dir() / "known_hosts"
        if known_hosts_path.exists():
            try:
                client.load_host_keys(str(known_hosts_path))
            except Exception:
                pass
        client.set_missing_host_key_policy(_TofuHostKeyPolicy(known_hosts_path))

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
            return ("認証失敗: 指定した鍵がユーザー %s では受け付けられません"
                    "でした。機器側の authorized_keys にこの鍵の公開鍵が"
                    "登録されているか、ユーザー名が合っているかを"
                    "確認してください。" % self.username)
        return "認証失敗: ユーザー名またはパスワードが間違っています"

    def connect(self) -> bool:
        """
        SSH接続を開始
        
        Returns:
            bool: 接続成功時True
        """
        try:
            self.client = paramiko.SSHClient()
            self._setup_host_keys(self.client)
            
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
                            self.error_occurred.emit(
                                "秘密鍵の読み込みエラー: この鍵はパスフレーズで保護されています。"
                                "パスフレーズ無しの鍵を指定してください。")
                        else:
                            self.error_occurred.emit(
                                "秘密鍵の読み込みエラー: 対応する鍵タイプが見つかりません。\n"
                                + "\n".join(key_errors))
                        return False

                    connect_kwargs['pkey'] = key
                    # 指定された鍵だけを使う。True にすると、その鍵が拒否された
                    # ときに ~/.ssh の別の鍵で認証が通ってしまい、利用者が
                    # 意図したのと違う身元で接続することになる。
                    connect_kwargs['look_for_keys'] = False
                except Exception as e:
                    self.error_occurred.emit(f"秘密鍵の読み込みエラー: {str(e)}")
                    return False
            elif self.password:
                connect_kwargs['password'] = self.password
            else:
                self.error_occurred.emit("パスワードまたは秘密鍵が必要です")
                return False
            
            # SSH接続を実行
            self.client.connect(**connect_kwargs)
            
            # インタラクティブシェルを開始
            self.channel = self.client.invoke_shell(term='vt100', width=80, height=24)
            self.channel.settimeout(0.1)
            
            self.is_connected = True
            self.connected.emit()
            
            # 読み取りスレッドを開始
            self._stop_reading = False
            self._read_thread = threading.Thread(target=self._read_output, daemon=True)
            self._read_thread.start()
            
            return True
            
        except paramiko.AuthenticationException:
            self.error_occurred.emit(self._auth_failure_message())
            return False
        except paramiko.BadHostKeyException:
            self.error_occurred.emit(
                "ホストキーが変更されています(中間者攻撃の可能性)。"
                "意図的な変更の場合は ~/.netbelt/known_hosts の該当ホスト行を削除してください。"
            )
            return False
        except paramiko.SSHException as e:
            self.error_occurred.emit(f"SSH接続エラー: {str(e)}")
            return False
        except Exception as e:
            self.error_occurred.emit(f"接続エラー: {str(e)}")
            return False
    
    def disconnect(self):
        """SSH接続を切断"""
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
            self.channel.send(command.encode('utf-8'))
        except Exception as e:
            self.error_occurred.emit(f"送信エラー: {str(e)}")
    
    def _read_output(self):
        """バックグラウンドで出力を読み取る"""
        while not self._stop_reading and self.is_connected:
            try:
                if self.channel and self.channel.recv_ready():
                    data = self.channel.recv(4096)
                    if data:
                        try:
                            text = data.decode('utf-8', errors='replace')
                            # リアルタイムで出力（バッファリングなし）
                            self.output_received.emit(text)
                        except UnicodeDecodeError:
                            pass
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
