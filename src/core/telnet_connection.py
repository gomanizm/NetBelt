"""Telnet接続管理"""
import codecs
import socket
import threading
import time
from typing import Optional
from PyQt6.QtCore import QObject, pyqtSignal

from .send_backpressure import DrainWatcher, socket_writable, wait_writable
from .sockets import tcp_port_number


class TelnetConnection(QObject):
    """Telnet接続を管理するクラス"""
    
    # シグナル定義
    output_received = pyqtSignal(str)  # 出力を受信
    connected = pyqtSignal()  # 接続成功
    disconnected = pyqtSignal()  # 切断
    error_occurred = pyqtSignal(str)  # エラー発生
    # 待たずに書けなかったソケットが、また書けるようになった（端末の
    # resume_send_queue へ繋ぐ）。見張りのスレッドから出すのでキュー接続で届く
    send_drained = pyqtSignal()
    
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
        # dispose() 済み（このオブジェクトは捨てられた）ことを覚えておく印。
        # _stop_reading と違い connect() の入口で戻さないので、接続スレッドが
        # 動き出す前に着地した dispose() でも消えない（SSH・シリアルと同じ）
        self._disposed = False
        # 画面の描き待ちが多すぎる間、受信を止めておくための関所
        # （TerminalWidget.output_gate。set_read_gate で受け取る）
        self._read_gate = None
        # ソケットへの書き込み（送信と交渉の応答）を直列にする錠。受信
        # スレッドの応答が、送っている区切りの途中へ割り込んで分かれないように
        self._write_lock = threading.Lock()
        # 送信の背圧（has_pending_sends）。接続が成立したときに、その
        # ソケットに束縛して作る
        self._drain_watcher: Optional[DrainWatcher] = None

    def set_read_gate(self, gate) -> None:
        """受信を止める合図（threading.Event）を受け取る

        set されている間だけソケットから読む。閉じている間は読まないので
        OS の受信バッファが埋まり、TCP のウィンドウが閉じて機器側が送るのを
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


    def connect(self) -> bool:
        """
        Telnet接続を開始
        
        Returns:
            bool: 接続成功時True
        """
        try:
            if self._disposed:
                # 接続スレッドが動き出す前にタブが閉じられ、dispose() が先に
                # 走った。ここで印を無視して進むと、TCP は最後まで張られる
                # のに参照しているものが誰もいない状態になり、閉じる経路が
                # 無いまま機器の vty 枠を掴んだままになる
                return False

            # 手編集の config.json などで "23" と文字列になっていると、
            # socket.connect は原因の分からない英語の例外で失敗し、true は
            # 1 番へ繋ぎに行く。SSH と同じ読み方で整数へそろえ、使えない値
            # なら機器へは何も繋がずに止める
            port = tcp_port_number(self.port)
            if port is None:
                self.error_occurred.emit(
                    "ポート番号 %r は使えないため、接続しませんでした。"
                    "機器の編集で 1〜65535 の整数を設定してください。"
                    % (self.port,))
                return False
            self.port = port

            # ソケット作成
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(10)
            
            # 接続
            self.socket.connect((self.host, self.port))
            
            # タイムアウトを短く設定（ノンブロッキング読み取り用）
            self.socket.settimeout(0.1)
            
            # 背圧の見張りは、このソケットに束縛する（後の接続のソケットを見ない）
            sock = self.socket
            self._drain_watcher = DrainWatcher(
                lambda: self._send_backlogged(sock), self._announce_drained,
                wait=lambda: wait_writable(sock, 0.05))
            
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

        これを呼んだあとの connect() は、何もせず False を返す。呼び出し元
        （MainWindow._close_connection / _dispose_connection）は dispose() の
        前に接続辞書からこのオブジェクトを外しており、以後この接続を使う人は
        いないため。同じオブジェクトで繋ぎ直す場合は disconnect() を使う。
        """
        self._disposed = True
        self._stop_reading = True
        self.is_connected = False

        # 送信の背圧の見張りを止める。相手が読まないまま詰まっていても、
        # ここで終わる（以後は send_drained を出さない）
        watcher, self._drain_watcher = self._drain_watcher, None
        if watcher is not None:
            watcher.stop()

        if self._read_thread and self._read_thread.is_alive():
            self._read_thread.join(timeout=2)

        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None

    def disconnect(self):
        """Telnet接続を切断（同じオブジェクトで繋ぎ直せる）"""
        self.dispose()
        # dispose() の印は「このオブジェクトは捨てた」意味なので、利用者が
        # 明示的に切断しただけの場合は消す。残すと次の connect() が
        # 取り消し扱いになり、繋ぎ直せなくなる
        self._disposed = False
        self.disconnected.emit()
    
    def send_command(self, command: str):
        """
        コマンドを送信（キー入力をそのまま送信）
        
        制限（既知・意図的）: NVT（RFC 854）では CR の後に LF か NUL を
        付けるが、ここはキー入力をそのまま流すので Enter は CR 単独
        （0x0d）で出る。Cisco IOS や netkit telnetd は CR 単独で行を
        確定するため実機では顕在化しない。CR LF へ変換すると、LF を
        別の改行として扱う機器で Enter のたびに空行が増えるので、
        実機で確かめられるまで変えない。
        
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
            # 端末は has_pending_sends が False のとき（ソケットへ待たずに
            # 書けるとき）だけ渡すので、ここは待たずに終わる。錠は受信
            # スレッドの交渉の応答と書き込みを混ぜないため
            with self._write_lock:
                self.socket.sendall(command.encode('utf-8'))
        except socket.error as e:
            self.error_occurred.emit(f"送信エラー: {str(e)}")
            if self.is_connected:
                self.is_connected = False
                self.disconnected.emit()
        except Exception as e:
            self.error_occurred.emit(f"送信エラー: {str(e)}")
    
    def has_pending_sends(self) -> bool:
        """いま区切りを渡されても、待たずには書けないか（端末の set_send_backlog 用）

        相手の読むのが遅いと OS の送信バッファが空かず、sendall は
        ソケットの時間切れ（0.1 秒）で途中までしか送れずに失敗する。どこまで
        送れたかは分からないので、切断として扱うしかなかった。書けない間は
        端末に次を渡させず、未送信の分を端末の列に残す。書けるようになったら
        send_drained で知らせる。
        """
        watcher = self._drain_watcher
        return watcher is not None and watcher.check()

    def _send_backlogged(self, sock) -> bool:
        """sock へ待たずに書けないなら True

        交渉の応答を書いている最中（錠が取られている）も True にする。
        そこで send_command を呼ぶと、GUI スレッドが錠で待たされる。
        閉じたとき・判定できないときは False（送らせれば送信エラーとして知らせる）。
        """
        if not self.is_connected:
            return False
        if self._write_lock.locked():
            return True
        return not socket_writable(sock)

    def _announce_drained(self):
        """見張りのスレッドから、また書けるようになったことを知らせる"""
        try:
            self.send_drained.emit()
        except RuntimeError:
            pass   # 捨てられた接続（C++ 側が消えている）

    def _read_output(self):
        """バックグラウンドで出力を読み取る"""
        # UTF-8 の途中で切れた分はデコーダの中に残り、次の受信と繋がる。
        # 溜めて閾値で強制復号すると、先頭バイトだけが化けたうえ、続きの
        # プロンプトが次に閾値を超えるまで画面に出なかった
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        pending = b''     # 途中で切れた制御シーケンス（次の受信と繋げる）

        while not self._stop_reading and self.is_connected:
            try:
                if self._wait_while_gated():
                    continue
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

        制限（既知・意図的）: オプションは一律に拒否する（DO には
        WONT、WILL には DONT）。Python の telnetlib と同じ方針で、
        状態を持たなくても再交渉のループに陥らない。ただし端末側の
        ローカルエコーも持たないため、RFC 857 どおり DONT ECHO を
        受けてエコーを止める機器では、入力中の文字が画面に出ない。
        Cisco IOS や Linux の telnetd はエコーを pty／回線側で行う
        のでこの条件には当たらない。WILL ECHO へ DO を返す方式に
        変えるなら、素朴な実装との交渉ループを防ぐ状態管理
        （RFC 1143 の Q 法）が要る。実機で確かめられるまで変えない。

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

        交渉の応答は3バイトで1つの意味を持つ。send は送れたバイト数を
        返すだけなので、途中までしか出ないと相手から見て交渉が成立せず、
        残りは次の送信にくっついて本文として届く（WONT と DONT が
        ff ff ＝エスケープされた 0xFF 1バイトに化ける）。送れたバイト数で
        位置を進めて送り切る。

        貼り付けの途中などで送信バッファが埋まっていると、0.1 秒の時間切れに
        なる。sendall だとどこまで送れたかが分からず、切断として扱うしか
        なかった。相手が詰まっているだけの時間切れは切断にせず、同じ位置から
        送り直す（切断・後始末されたら諦める）。送信（send_command）と同じ
        錠の中で書くので、送っている区切りの途中へ割り込まない。

        Args:
            command: 送信するコマンド
        """
        if self.socket and self.is_connected:
            try:
                with self._write_lock:
                    self._send_through(self.socket, command)
            except OSError as e:
                # 裸の except は KeyboardInterrupt まで飲む。送信の失敗は
                # OSError（socket.error を含む。時間切れは _send_through が
                # 送り直す）だけを捕まえ、黙って落とさず操作者へ知らせる。
                self.error_occurred.emit(f"Telnet交渉の応答を送信できませんでした: {str(e)}")

    def _send_through(self, sock, data: bytes):
        """data を全部書く。時間切れは切断にせず、同じ位置から送り直す"""
        view = memoryview(data)
        while view:
            try:
                sent = sock.send(view)
            except socket.timeout:
                if self._stop_reading or not self.is_connected:
                    return    # 切断・後始末された。詰まった相手を待ち続けない
                continue
            view = view[sent:]
