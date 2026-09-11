"""
シリアルポート接続クラス
"""
import codecs
import serial
import serial.tools.list_ports
from PyQt6.QtCore import QObject, pyqtSignal
from typing import List, Dict, Optional
import queue
import threading
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
        self._read_thread: Optional[threading.Thread] = None
        # 送信は GUI スレッドを止めないよう、専用スレッドがキューから書く
        self._send_queue: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._write_thread: Optional[threading.Thread] = None
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
            
            # 開いている最中に dispose() されたことを、開き終わってから
            # 知るための印。ここで戻しておき、生成後にもう一度見る
            self._should_stop = False

            # シリアルポートを開く
            port = serial.Serial(
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

            if self._should_stop:
                # 開いている間にタブが閉じられた（または後始末が走った）。
                # このまま続けると、閉じる経路の無いポートが開いたまま残り、
                # 同じ COM への再接続が Access is denied になる
                port.close()
                return False

            self.serial_conn = port
            self._is_connected = True
            
            # 接続成功メッセージ
            self.output_received.emit(
                f"\r\n接続しました: {self.port} ({self.baudrate} baud)\r\n")
            self.connected.emit()
            
            # 読み取りスレッドを開始
            self._start_read_thread()
            
            return True
            
        except serial.SerialException as e:
            error_msg = f"接続失敗: {str(e)}"
            self.output_received.emit(f"\r\n{error_msg}\r\n")
            self.error_occurred.emit(error_msg)
            return False
        except Exception as e:
            error_msg = f"予期しないエラー: {str(e)}"
            self.output_received.emit(f"\r\n{error_msg}\r\n")
            self.error_occurred.emit(error_msg)
            return False
    
    def dispose(self):
        """ポートを閉じて資源を手放す（切断の通知は出さない）

        機器側都合の切断や読み取りエラーを受けたあとの後始末で使う。
        ここで disconnected を出すと、いま処理中の切断処理がもう一度
        呼ばれて案内や後始末が二重になる。

        Windows の COM ポートは同一プロセス内でも排他なので、閉じずに
        参照だけ捨てると、同じ機器への再接続が Access is denied になる。
        """
        self._should_stop = True
        self._is_connected = False

        # 読み取りスレッドの終了を待つ（自分自身からの後始末では待てない）
        thread = self._read_thread
        if (thread is not None and thread.is_alive()
                and thread is not threading.current_thread()):
            thread.join(timeout=2)
        self._read_thread = None

        # 接続が存在する場合は閉じる
        if self.serial_conn and self.serial_conn.is_open:
            try:
                self.serial_conn.close()
            except Exception as e:
                print(f"切断エラー: {e}")

        self.serial_conn = None

        # 送信スレッドを終わらせる。溜まっている分は捨て、次の接続には
        # 新しいキューを使う（古い終端印が次の送信を巻き込まないように）
        old_queue = self._send_queue
        self._send_queue = queue.Queue()
        old_queue.put(None)
        writer = self._write_thread
        if (writer is not None and writer.is_alive()
                and writer is not threading.current_thread()):
            writer.join(timeout=2)
        self._write_thread = None

    def disconnect(self):
        """シリアルポートから切断"""
        self.dispose()
        self.disconnected.emit()
    
    def _start_read_thread(self):
        """読み取りスレッドを開始"""
        # 後始末で終了を待てるよう、スレッドを保持する
        self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._read_thread.start()

    def _read_loop(self):
        """データを継続的に読み取る"""
        # 受信の切れ目で割れた多バイト文字を、次の受信と繋いで復号する。
        # in_waiting > 0 で即読むので、9600bps では 3 バイト文字の途中で
        # 読むことが多く、受信ごとに復号すると日本語が頻繁に化ける
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        while self._is_connected and not self._should_stop:
            try:
                if self.serial_conn and self.serial_conn.is_open and self.serial_conn.in_waiting > 0:
                    # データを読み取り
                    data = self.serial_conn.read(self.serial_conn.in_waiting)

                    # デコードして出力
                    try:
                        text = decoder.decode(data)
                        if text:
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
        # 切れ目で終わった未完の文字を捨てない
        rest = decoder.decode(b'', final=True)
        if rest:
            self.output_received.emit(rest)
    
    def send_command(self, command: str):
        """
        コマンドを送信
        
        Args:
            command: 送信するコマンド
        """
        if not self._is_connected or not self.serial_conn or not self.serial_conn.is_open:
            self.error_occurred.emit("送信エラー: 接続されていません")
            return

        # GUI スレッドで write/flush を同期実行すると、送り終えるまで
        # 画面が止まる（9600 baud で 512 文字 ≈ 0.5 秒、300 baud ≈ 17 秒）。
        # 送信スレッドへ積んで、ここではすぐ戻る。順序はキューが保つ
        # コマンドを送信（改行は含めない - ターミナル側で処理済み）
        self._ensure_write_thread()
        self._send_queue.put(command.encode('utf-8'))

    def _ensure_write_thread(self):
        """送信スレッドが無ければ起こす（最初の送信時、または再接続後）"""
        thread = self._write_thread
        if thread is not None and thread.is_alive():
            return
        self._write_thread = threading.Thread(
            target=self._write_loop, args=(self._send_queue,), daemon=True)
        self._write_thread.start()

    def _write_loop(self, send_queue):
        """キューに積まれた送信を順に書く。None で終わる"""
        while True:
            data = send_queue.get()
            if data is None:
                break
            port = self.serial_conn
            if self._should_stop or port is None or not port.is_open:
                continue
            try:
                port.write(data)
                port.flush()
            except serial.SerialException as e:
                # 後始末で閉じられた直後の失敗は、切断済みなので知らせない
                if self._should_stop:
                    continue
                self.error_occurred.emit(f"送信エラー: {str(e)}")
                self._is_connected = False
            except Exception as e:
                if self._should_stop:
                    continue
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
