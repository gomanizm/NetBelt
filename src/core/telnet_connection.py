"""Telnet接続管理"""
import codecs
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
        # 大きすぎるサブネゴシエーションを捨てた後、その終端 (IAC SE) が
        # 来るまで読み飛ばし続けるための状態。持ち越しを空にするだけだと
        # 「いま SB の途中にいる」ことまで忘れ、本体の続きが画面へ漏れる
        self._discarding_sb = False
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
    
    def dispose(self):
        """ソケットを閉じて資源を手放す（切断の通知は出さない）

        機器側都合の切断やエラーを受けたあとの後始末で使う。ここで
        disconnected を出すと、いま処理中の切断処理が再入する。
        """
        self._stop_reading = True
        self.is_connected = False

        if self._read_thread and self._read_thread.is_alive():
            self._read_thread.join(timeout=2)

        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None

    def disconnect(self):
        """Telnet接続を切断"""
        self.dispose()
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
            # send は送れたバイト数を返すだけで、渡した全部を送ったとは
            # 限らない。貼り付けをまとめて渡すようになったので、
            # 残りを送り切る sendall を使う。
            self.socket.sendall(command.encode('utf-8'))
        except socket.error as e:
            self.error_occurred.emit(f"送信エラー: {str(e)}")
            if self.is_connected:
                self.is_connected = False
                self.disconnected.emit()
        except Exception as e:
            self.error_occurred.emit(f"送信エラー: {str(e)}")
    
    def _read_output(self):
        """バックグラウンドで出力を読み取る"""
        # UTF-8 の途中で切れた分はデコーダの中に残り、次の受信と繋がる。
        # 溜めて閾値で強制復号すると、先頭バイトだけが化けたうえ、続きの
        # プロンプトが次に閾値を超えるまで画面に出なかった
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        pending = b''     # 途中で切れた制御シーケンス（次の受信と繋げる）

        while not self._stop_reading and self.is_connected:
            try:
                if self.socket:
                    try:
                        data = self.socket.recv(4096)
                        if data:
                            # Telnet制御シーケンスの処理。途中で切れた分は
                            # pending に残し、次の受信の先頭へ繋ぐ
                            clean, pending = self._process_telnet_commands(
                                pending + data)
                            text = decoder.decode(clean)
                            if text:
                                self.output_received.emit(text)
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
        # 切れ目で終わった未完の文字を捨てない
        rest = decoder.decode(b'', final=True)
        if rest:
            self.output_received.emit(rest)

    # 未完のシーケンスを持ち越す上限。壊れた相手が IAC SB を送り続けて
    # 終端を寄こさない場合に、際限なく溜め込まないようにする。
    # 実際のサブネゴシエーションは数十バイトで収まる。
    MAX_PENDING_BYTES = 4096

    @staticmethod
    def _find_sb_end(data: bytes, start: int):
        """サブネゴシエーションの終端 IAC SE を探す。

        `find(b"\\xff\\xf0")` では足りない。本文の中の 0xFF は IAC IAC と
        エスケープされて届くので、`0xFF 0xFF 0xF0` が並ぶと 2 つ目の
        0xFF と 0xF0 を終端と誤認し、残りの本文が画面へ漏れる。
        IAC の次のバイトを見て 1 組ずつ進める。

        Returns:
            (終端 IAC の位置, 末尾に残った対の無い IAC の数)。
            終端が無ければ位置は None。末尾が IAC 単独で終わっている
            場合は 1 を返すので、呼び出し側はそのバイトを次の受信へ
            持ち越せる（次の受信の先頭が SE や IAC と対になる）。
        """
        IAC, SE = 255, 240
        j = start
        while j < len(data):
            if data[j] != IAC:
                j += 1
                continue
            if j + 1 >= len(data):
                return None, 1
            if data[j + 1] == SE:
                return j, 0
            # IAC IAC（エスケープ）や、本文中のその他の IAC x は 1 組で飛ばす
            j += 2
        return None, 0

    def _process_telnet_commands(self, data: bytes):
        """
        Telnet制御コマンドを処理

        シーケンスは TCP の切れ目をまたぐ。途中で切れた分をその場で
        捨てたり通常データとして出したりすると、0xFF が画面へ漏れて
        UTF-8 デコードを壊し、応答も返せずに機器が待ち続ける。
        揃っていない分は次の受信まで持ち越す。

        Args:
            data: 受信データ（前回の持ち越しを先頭に連結したもの）

        Returns:
            (画面へ出すデータ, 次の受信へ持ち越す未完のシーケンス)
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
        pending = b''
        i = 0

        # 直前に大きすぎるサブネゴシエーションを捨てていたら、その終端が
        # 来るまで読み飛ばす。ここで普通のデータとして扱うと、本体の続きが
        # 画面へ漏れ、終端の IAC SE だけがコマンドとして消費されて
        # 以後の解釈もずれる。
        if self._discarding_sb:
            end, dangling = self._find_sb_end(data, 0)
            if end is None:
                # 終端がまだ来ない。末尾が IAC 単独なら次の受信へ持ち越す。
                # 捨てると、次の受信が SE で始まる（終端が切れ目で割れた）
                # ときに二度と再同期できず、以後の受信を全部捨て続ける
                if dangling:
                    return b'', data[-dangling:]
                return b'', b''
            self._discarding_sb = False
            data = data[end + 2:]

        while i < len(data):
            if data[i] != IAC:
                # 通常のデータ
                output.append(data[i])
                i += 1
                continue

            # ここから IAC。揃っていなければ次の受信まで持ち越す。
            # 通常データとして出すと 0xFF が画面へ漏れる。
            if i + 1 >= len(data):
                pending = data[i:]
                break

            cmd = data[i + 1]

            if cmd == IAC:
                # IAC IAC = エスケープされた 0xFF
                output.append(IAC)
                i += 2
            elif cmd in (DO, DONT, WILL, WONT):
                # 3バイトコマンド: IAC + CMD + OPTION
                if i + 2 >= len(data):
                    # オプションが次の受信に入っている。捨てると応答も
                    # 返せず、待っている機器はプロンプトを出さない
                    pending = data[i:]
                    break
                option = data[i + 2]
                # 基本的な応答: DOに対してWONT、WILLに対してDONT
                if cmd == DO:
                    # 要求された機能を拒否
                    self._send_telnet_command(bytes([IAC, WONT, option]))
                elif cmd == WILL:
                    # 提案された機能を拒否
                    self._send_telnet_command(bytes([IAC, DONT, option]))
                i += 3
            elif cmd == SB:
                # サブネゴシエーション: IAC SB ... IAC SE まで読み飛ばす。
                # 本文の IAC IAC を終端と見誤らないよう、1 組ずつ進める
                end, _ = self._find_sb_end(data, i + 2)
                if end is None:
                    # 終端がまだ来ていない。破棄すると以降の本文まで失う
                    pending = data[i:]
                    break
                i = end + 2
            else:
                # その他のコマンドは2バイトとして扱う
                i += 2

        if len(pending) > self.MAX_PENDING_BYTES:
            # 終端を寄こさない相手。溜め込み続けるより捨てる。
            # ただし SB の途中なら、終端が来るまで読み飛ばす状態を残す。
            # 空にするだけだと、続きを通常データとして画面へ出してしまう。
            print("[Telnet] 未完の制御シーケンスが大きすぎるため破棄しました")
            self._discarding_sb = pending[:2] == bytes([IAC, SB])
            if self._discarding_sb:
                # 捨てる分の末尾が対の無い IAC なら、それだけは次の受信へ
                # 持ち越す。ここで捨てると、次の受信が SE で始まった
                # （終端が切れ目で割れた）ときに終端を見つけられず、
                # 以後の受信を全部捨て続ける。末尾 1 バイトで判定すると
                # 本文中の IAC IAC まで持ち越して次の SE を終端と誤認
                # するので、_find_sb_end の dangling で数える。
                _, dangling = self._find_sb_end(pending, 2)
                pending = pending[-dangling:] if dangling else b''
            else:
                pending = b''

        return bytes(output), pending
    
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
