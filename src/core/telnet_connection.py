"""Telnet接続管理"""
import socket
import threading
import time
from typing import Optional
from PyQt6.QtCore import QObject, pyqtSignal


class TelnetConnection(QObject):
    """Telnet接続を管理するクラス"""
    
    # シグナル定義
    output_received = pyqtSignal(str)  # 出力を受信
    connected = pyqtSignal()  # 接続成功
    disconnected = pyqtSignal()  # 切断
    error_occurred = pyqtSignal(str)  # エラー発生
    
    def __init__(self, host: str, port: int, username: str = "", password: str = "", 
                 parent=None):
        """
        初期化
        
        Args:
            host: ホスト名またはIPアドレス
            port: ポート番号（デフォルト: 23）
            username: ユーザー名（オプション）
            password: パスワード（オプション）
            parent: 親オブジェクト
        """
        super().__init__(parent)
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        
        self.socket: Optional[socket.socket] = None
        self.is_connected = False
        self._read_thread: Optional[threading.Thread] = None
        self._stop_reading = False
    
    def connect(self) -> bool:
        """
        Telnet接続を開始
        
        Returns:
            bool: 接続成功時True
        """
        try:
            # ソケット作成
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(10)
            
            # 接続
            self.socket.connect((self.host, self.port))
            
            # タイムアウトを短く設定（ノンブロッキング読み取り用）
            self.socket.settimeout(0.1)
            
            self.is_connected = True
            self.connected.emit()
            
            # 読み取りスレッドを開始
            self._stop_reading = False
            self._read_thread = threading.Thread(target=self._read_output, daemon=True)
            self._read_thread.start()
            
            # 自動ログイン処理（オプション）
            if self.username or self.password:
                # ユーザー名とパスワードは初期接続後に手動で入力される想定
                # 自動ログインが必要な場合は、ここでプロンプトを待って送信する
                pass
            
            return True
            
        except socket.timeout:
            self.error_occurred.emit(f"接続タイムアウト: {self.host}:{self.port}")
            return False
        except socket.error as e:
            self.error_occurred.emit(f"接続エラー: {str(e)}")
            return False
        except Exception as e:
            self.error_occurred.emit(f"予期しないエラー: {str(e)}")
            return False
    
    def disconnect(self):
        """Telnet接続を切断"""
        self._stop_reading = True
        self.is_connected = False
        
        if self._read_thread and self._read_thread.is_alive():
            self._read_thread.join(timeout=2)
        
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None
        
        self.disconnected.emit()
    
    def send_command(self, command: str):
        """
        コマンドを送信（キー入力をそのまま送信）
        
        Args:
            command: 送信するコマンド（1文字または制御文字）
        """
        if not self.is_connected or not self.socket:
            return
        
        try:
            # キー入力をそのまま送信
            # Enterキーは'\r'として送られてくる
            self.socket.send(command.encode('utf-8'))
        except socket.error as e:
            self.error_occurred.emit(f"送信エラー: {str(e)}")
            if self.is_connected:
                self.is_connected = False
                self.disconnected.emit()
        except Exception as e:
            self.error_occurred.emit(f"送信エラー: {str(e)}")
    
    def _read_output(self):
        """バックグラウンドで出力を読み取る"""
        buffer = b''
        
        while not self._stop_reading and self.is_connected:
            try:
                if self.socket:
                    try:
                        data = self.socket.recv(4096)
                        if data:
                            buffer += data
                            
                            # Telnet制御シーケンスの処理
                            buffer = self._process_telnet_commands(buffer)
                            
                            # バッファ内のデータをデコードして送信
                            if buffer:
                                try:
                                    # 完全なUTF-8文字が揃っているか確認
                                    text = buffer.decode('utf-8')
                                    self.output_received.emit(text)
                                    buffer = b''
                                except UnicodeDecodeError:
                                    # 不完全なUTF-8シーケンスの場合はバッファに保持
                                    # 最大バッファサイズチェック（メモリリーク防止）
                                    if len(buffer) > 100:
                                        # 強制的にデコード
                                        text = buffer.decode('utf-8', errors='replace')
                                        self.output_received.emit(text)
                                        buffer = b''
                        else:
                            # データがない場合は接続が閉じられた
                            if self.is_connected:
                                self.is_connected = False
                                self.disconnected.emit()
                            break
                    except socket.timeout:
                        # タイムアウトは正常（ノンブロッキング読み取り）
                        time.sleep(0.01)
                    except socket.error:
                        # ソケットエラーは切断
                        if self.is_connected:
                            self.is_connected = False
                            self.disconnected.emit()
                        break
                else:
                    break
                    
            except Exception as e:
                if self.is_connected:
                    self.error_occurred.emit(f"読み取りエラー: {str(e)}")
                    self.is_connected = False
                    self.disconnected.emit()
                break
    
    def _process_telnet_commands(self, data: bytes) -> bytes:
        """
        Telnet制御コマンドを処理
        
        Args:
            data: 受信データ
            
        Returns:
            bytes: 制御コマンドを除去したデータ
        """
        # Telnet制御シーケンス
        # IAC (Interpret As Command) = 0xFF (255)
        IAC = 255
        DONT = 254
        DO = 253
        WONT = 252
        WILL = 251
        SB = 250  # Subnegotiation Begin
        SE = 240  # Subnegotiation End
        
        output = bytearray()
        i = 0
        
        while i < len(data):
            if data[i] == IAC and i + 1 < len(data):
                # IAC コマンド処理
                cmd = data[i + 1]
                
                if cmd == IAC:
                    # IAC IAC = エスケープされた 0xFF
                    output.append(IAC)
                    i += 2
                elif cmd in (DO, DONT, WILL, WONT):
                    # 3バイトコマンド: IAC + CMD + OPTION
                    if i + 2 < len(data):
                        option = data[i + 2]
                        # 基本的な応答: DOに対してWONT、WILLに対してDONT
                        if cmd == DO:
                            # 要求された機能を拒否
                            self._send_telnet_command(bytes([IAC, WONT, option]))
                        elif cmd == WILL:
                            # 提案された機能を拒否
                            self._send_telnet_command(bytes([IAC, DONT, option]))
                        i += 3
                    else:
                        i += 2
                elif cmd == SB:
                    # サブネゴシエーション: IAC SB ... IAC SE まで読み飛ばす
                    j = i + 2
                    while j < len(data) - 1:
                        if data[j] == IAC and data[j + 1] == SE:
                            i = j + 2
                            break
                        j += 1
                    else:
                        # SE が見つからない場合は残りを破棄
                        i = len(data)
                else:
                    # その他のコマンドは2バイトとして扱う
                    i += 2
            else:
                # 通常のデータ
                output.append(data[i])
                i += 1
        
        return bytes(output)
    
    def _send_telnet_command(self, command: bytes):
        """
        Telnetコマンドを送信
        
        Args:
            command: 送信するコマンド
        """
        if self.socket and self.is_connected:
            try:
                self.socket.send(command)
            except:
                pass
