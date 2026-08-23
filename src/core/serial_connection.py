"""
シリアルポート接続クラス
"""
import serial
import serial.tools.list_ports
from PyQt6.QtCore import QObject, pyqtSignal, QThread
from typing import List, Dict, Optional
import time


class SerialConnection(QObject):
    """シリアルポート接続を管理するクラス"""
    
    # シグナル定義
    output_received = pyqtSignal(str)  # 受信データ
    connected = pyqtSignal()           # 接続成功
    disconnected = pyqtSignal()        # 切断
    error_occurred = pyqtSignal(str)   # エラー発生
    
    def __init__(self, port: str, baudrate: int = 9600, parent: Optional[QObject] = None):
        """
        初期化
        
        Args:
            port: ポート名（例: COM3, /dev/ttyUSB0）
            baudrate: ボーレート（デフォルト: 9600）
            parent: 親オブジェクト
        """
        super().__init__(parent)
        self.port = port
        self.baudrate = baudrate
        self.serial_conn: Optional[serial.Serial] = None
        self._read_thread: Optional[QThread] = None
        self._is_connected = False
        self._should_stop = False
    
    def connect(self) -> bool:
        """
        シリアルポートに接続
        
        Returns:
            接続に成功した場合True、失敗した場合False
        """
        try:
            # 既に接続されている場合は切断
            if self._is_connected:
                self.disconnect()
            
            # シリアルポートを開く
            self.serial_conn = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=1,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False
            )
            
            self._is_connected = True
            self._should_stop = False
            
            # 接続成功メッセージ
            self.output_received.emit(f"\n接続しました: {self.port} ({self.baudrate} baud)\n")
            self.connected.emit()
            
            # 読み取りスレッドを開始
            self._start_read_thread()
            
            return True
            
        except serial.SerialException as e:
            error_msg = f"接続失敗: {str(e)}"
            self.output_received.emit(f"\n{error_msg}\n")
            self.error_occurred.emit(error_msg)
            return False
        except Exception as e:
            error_msg = f"予期しないエラー: {str(e)}"
            self.output_received.emit(f"\n{error_msg}\n")
            self.error_occurred.emit(error_msg)
            return False
    
    def disconnect(self):
        """シリアルポートから切断"""
        self._should_stop = True
        self._is_connected = False
        
        # 接続が存在する場合は閉じる
        if self.serial_conn and self.serial_conn.is_open:
            try:
                self.serial_conn.close()
            except Exception as e:
                print(f"切断エラー: {e}")
        
        self.serial_conn = None
        self.disconnected.emit()
    
    def _start_read_thread(self):
        """読み取りスレッドを開始"""
        import threading
        
        def read_loop():
            """データを継続的に読み取る"""
            while self._is_connected and not self._should_stop:
                try:
                    if self.serial_conn and self.serial_conn.is_open and self.serial_conn.in_waiting > 0:
                        # データを読み取り
                        data = self.serial_conn.read(self.serial_conn.in_waiting)
                        
                        # デコードして出力
                        try:
                            text = data.decode('utf-8', errors='replace')
                            self.output_received.emit(text)
                        except Exception as e:
                            self.error_occurred.emit(f"デコードエラー: {str(e)}")
                    else:
                        # データがない場合は少し待つ
                        time.sleep(0.01)
                        
                except serial.SerialException as e:
                    self.error_occurred.emit(f"読み取りエラー: {str(e)}")
                    self._is_connected = False
                    break
                except Exception as e:
                    self.error_occurred.emit(f"予期しないエラー: {str(e)}")
                    break
        
        # スレッドを開始
        thread = threading.Thread(target=read_loop, daemon=True)
        thread.start()
    
    def send_command(self, command: str):
        """
        コマンドを送信
        
        Args:
            command: 送信するコマンド
        """
        if not self._is_connected or not self.serial_conn or not self.serial_conn.is_open:
            self.error_occurred.emit("送信エラー: 接続されていません")
            return
        
        try:
            # コマンドを送信（改行は含めない - ターミナル側で処理済み）
            self.serial_conn.write(command.encode('utf-8'))
            self.serial_conn.flush()
            
        except serial.SerialException as e:
            self.error_occurred.emit(f"送信エラー: {str(e)}")
            self._is_connected = False
        except Exception as e:
            self.error_occurred.emit(f"予期しないエラー: {str(e)}")
    
    @property
    def is_connected(self) -> bool:
        """接続状態を取得"""
        return self._is_connected


def list_serial_ports() -> List[Dict[str, str]]:
    """
    利用可能なシリアルポートをリストアップ
    
    Returns:
        シリアルポート情報のリスト
        各要素は {'port': ポート名, 'description': 説明, 'hwid': ハードウェアID}
    """
    ports = []
    
    for port in serial.tools.list_ports.comports():
        ports.append({
            'port': port.device,
            'description': port.description,
            'hwid': port.hwid
        })
    
    return ports
