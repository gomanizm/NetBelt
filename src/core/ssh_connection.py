"""SSH接続管理"""
import codecs
import os
import paramiko
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from PyQt6.QtCore import QObject, pyqtSignal

# known_hosts の保存を直列化する。同時に保存すると、あとから
# os.replace した側が先の結果を丸ごと差し替えてしまう
_known_hosts_save_lock = threading.Lock()


def _save_known_hosts(client, known_hosts_path):
    """client が持つホスト鍵を known_hosts へ書き戻す。

    paramiko 4.0.0 の SSHClient.save_host_keys は保存先を "w" で開いて
    先に切り詰めるため、書いている途中で落ちると保存済みの鍵をまとめて
    失う。しかも保存前の再読込は load_host_keys 済みの client でしか
    走らないので、known_hosts がまだ無い時点で始めた接続は、他の接続が
    先に保存した鍵を上書きして消す。消された機器は次回また「未知」に
    戻り、鍵が変わっていても確認なしで受け入れられる。

    書く直前に既存のファイルを読み直して自分の鍵と合流させ、一時
    ファイルへ書いてから os.replace で差し替える。差し替えは不可分な
    ので、途中で落ちても前の known_hosts がそのまま残る。
    """
    path = Path(str(known_hosts_path))
    with _known_hosts_save_lock:
        if path.exists():
            # 他の接続がこの間に保存した鍵を取り込む
            client.load_host_keys(str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        # os.replace はドライブを跨げないので一時ファイルは同階層に作る
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        os.close(fd)
        try:
            client.save_host_keys(tmp_path)
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
            _save_known_hosts(client, self._known_hosts_path)
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
        if known_hosts_path.exists():
            try:
                client.load_host_keys(str(known_hosts_path))
            except Exception as e:
                # 握りつぶして TOFU にすると、既知の機器でも「未知」扱いになり、
                # 鍵が変わっていても気づかずにパスワードを送る。検証できない
                # 状態で認証へ進まない
                raise HostKeyStoreError(
                    "既知ホスト鍵 (known_hosts) を読めないため接続を中止しました: %s\n%s\n"
                    "壊れた行が 1 つあるだけでも読めなくなります。該当行を修正または"
                    "削除するか、ファイルを退避してから接続し直してください"
                    "（退避すると全機器が初回接続の扱いになります）。"
                    % (e, known_hosts_path))
        policy = _TofuHostKeyPolicy(known_hosts_path)
        policy._on_save_error = lambda message: self.output_received.emit(
            "\r\n[NetBelt] 警告: %s\r\n" % message)
        client.set_missing_host_key_policy(policy)

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

    def _fail(self, message: str) -> bool:
        """接続に失敗したときの後始末と通知

        paramiko の SSHClient.connect() は失敗しても自分ではトランスポートを
        閉じない。閉じずに戻ると、機器へ張った TCP セッションと Transport
        スレッドが生き残る。呼び出し側も失敗時は disconnect() を呼ばないので、
        SSHConnection を捨てても Transport スレッド自身がオブジェクトを
        参照し続け、GC でも回収されない。

        機器側は認証前のログイン猶予（Cisco IOS の ip ssh time-out、
        OpenSSH の LoginGraceTime、いずれも既定 120 秒）でいずれ切るが、
        invoke_shell の失敗は認証が通ったあとなので猶予が効かない。
        """
        self.dispose()
        self.error_occurred.emit(message)
        return False

    def _abandon(self, client, channel) -> bool:
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
        return False

    def connect(self) -> bool:
        """
        SSH接続を開始

        Returns:
            bool: 接続成功時True
        """
        try:
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
                # dispose() が呼んだ close() は Transport 登録前で何もして
                # いないので、ここで閉じないと成立したセッションとスレッドが
                # 残り、機器の vty 枠を掴んだままになる
                return self._abandon(client, None)
            
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
                return self._abandon(client, channel)

            self.channel = channel
            self.is_connected = True
            self.connected.emit()
            
            # 読み取りスレッドを開始
            self._read_thread = threading.Thread(target=self._read_output, daemon=True)
            self._read_thread.start()
            
            return True
            
        except paramiko.AuthenticationException:
            return self._fail(self._auth_failure_message())
        except paramiko.BadHostKeyException:
            return self._fail(
                "ホストキーが変更されています(中間者攻撃の可能性)。"
                "意図的な変更の場合は ~/.netbelt/known_hosts の該当ホスト行を削除してください。"
            )
        except paramiko.SSHException as e:
            return self._fail(f"SSH接続エラー: {str(e)}")
        except Exception as e:
            return self._fail(f"接続エラー: {str(e)}")
    
    def dispose(self):
        """チャネルと SSHClient を閉じて資源を手放す（通知は出さない）

        機器側都合の切断やエラーを受けたあとの後始末で使う。ここで
        disconnected を出すと、いま処理中の切断処理が再入する。
        閉じずに参照だけ捨てると、Transport スレッド自身がオブジェクトを
        参照し続けるため GC でも回収されない。
        """
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
        """SSH接続を切断"""
        self.dispose()
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
