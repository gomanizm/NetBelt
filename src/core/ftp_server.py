"""FTP サーバー（pyftpdlib ラッパ）。UI 通知は Qt シグナル。"""
import threading
import time

from PyQt6.QtCore import QObject, pyqtSignal
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler, DTPHandler
from pyftpdlib.servers import FTPServer as _PyFTPServer
from pyftpdlib.ioloop import IOLoop as _PyIOLoop

from .crypto import PasswordCrypto

# 復号できずに暗号文のまま残ったパスワードを渡されたときの通知文。
# 暗号文そのものは含めない（画面・ログに出さない）。
UNDECRYPTABLE_PASSWORD_MESSAGE = (
    "保存されたパスワードを復号できないため、サーバーを起動しません。"
    "別の Windows アカウント/PC で保存された設定の可能性があります。"
    "パスワードを入力し直してください")

# 停止時の join が上限で諦めたとき、ソケットを閉じるのは待受スレッド自身
# なのでポートは掴まれたままになる。その状態での起動を断る理由の文言。
PREVIOUS_STOP_INCOMPLETE_MESSAGE = (
    "前回の停止が完了していません（待受スレッドが終了しておらず、"
    "ポートが解放されていない可能性があります）。"
    "しばらく待ってからもう一度お試しください")


class FTPServerManager(QObject):
    started = pyqtSignal()
    stopped = pyqtSignal()
    error_occurred = pyqtSignal(str)
    client_activity = pyqtSignal(str, str)      # ip, message
    # サイズは object で渡す。int だと C++ の 32bit int へ丸められ、
    # 2GiB 超の転送が例外も出さず小さい値や負値として表示される
    transfer_started = pyqtSignal(str, str, object, str)        # ip, filename, total, direction
    transfer_progress = pyqtSignal(str, str, object, object, str)  # ip, filename, done, total, direction
    transfer_complete = pyqtSignal(str, str, object, object, str)  # ip, filename, done, total, direction
    # 未完了で終わった転送（ABOR・接続断・停止）。TFTP と同じ口
    transfer_interrupted = pyqtSignal(str, str, str)            # ip, filename, direction

    def __init__(self, parent=None):
        super().__init__(parent)
        self._server = None
        self._thread = None
        self._stop_event = threading.Event()
        self.is_running = False
        self.port = 0
        # (ip, path, direction) 単位の表示コアレス。機器が1回の copy で複数FTP接続を張っても
        # 論理転送1本=履歴1行にする（Cisco IOS は本転送前に接続→RETR→即切断のプローブを複数回行う）。
        # path は実ファイルのパス（省略時は filename）、値はその転送の表示名。basename で
        # 束ねると、別ディレクトリの同名ファイルの並行転送が1本にまとめられる
        self._tx = {}
        # 同じ鍵で持つ行の状態: 開始を届けた（shown）／配送待ちの上限で
        # 省いた（hidden）。_tx の値は表示名なので、そこへは混ぜられない
        self._tx_row = {}
        # GUI へ渡したまま、まだ処理されていない通知の件数の上限。
        # 接続と切断のたびに 1 件出るので、認証の要らない相手が接続して即切断を
        # 繰り返すと、GUI が他の処理で塞がっている間に Qt の配送キューへ際限なく
        # 積み上がる（パネルのログの行数上限が効くのは配送の後）。TFTP の
        # protocol_event と同じく、超過中は数えるだけにして、はけた時点で
        # 省略した件数を 1 行だけ出す。数え違えないよう、この信号はすべて
        # _emit_activity から出す。
        # 転送の通知（開始・進捗・完了・中断）も同じカウンタに数える。
        # 数えないと、機器が小さいファイルの RETR を連打するだけで、
        # 接続・切断と同じようにキューへ積み上がる
        self.max_pending_notices = 1000
        self._notice_lock = threading.Lock()
        self._pending_notices = 0
        self._dropped_notices = 0
        # 自分の信号を自分でも受ける。待受スレッドから emit した分はキュー経由で
        # GUI スレッドへ届くので、呼ばれたことが「GUI が 1 件処理した」の合図
        for signal in (self.client_activity, self.transfer_started,
                       self.transfer_progress, self.transfer_complete,
                       self.transfer_interrupted):
            signal.connect(self._on_notice_delivered)

    def _take_notice(self, force=False, count_drop=True):
        """配送待ちを 1 件ぶん確保する。確保できたら True（呼び出し側が emit する）。

        上限に達している間は False。count_drop なら省略件数へ足す（進捗は
        次の進捗か完了で置き換わるので足さない）。force は上限を超えても
        確保する（開始を届けた転送の行を閉じる通知。捨てると行が残る）
        """
        with self._notice_lock:
            if not force and self._pending_notices >= self.max_pending_notices:
                if count_drop:
                    self._dropped_notices += 1
                return False
            self._pending_notices += 1
            return True

    def _take_closing_notice(self, row):
        """完了・中断を渡すかを決める。

        開始を届けた行を閉じる 1 件は必ず渡す。開始を省いた転送のものは
        渡さずに省略件数へ足す。行の無いもの（台帳に無い＝束ねた相手が
        先に閉じた）は上限の範囲でだけ渡す
        """
        if row == "shown":
            return self._take_notice(force=True)
        if row == "hidden":
            with self._notice_lock:
                self._dropped_notices += 1
            return False
        return self._take_notice()

    def _emit_activity(self, ip, message):
        """client_activity を 1 件渡す。配送待ちが上限に達している間は数えるだけ。"""
        if self._take_notice():
            self.client_activity.emit(ip, message)

    def _on_notice_delivered(self, *_args):
        """GUI が client_activity を 1 件処理したので配送待ちを戻す（GUI スレッドで動く）"""
        with self._notice_lock:
            if self._pending_notices > 0:
                self._pending_notices -= 1
            dropped = 0
            if self._pending_notices == 0:
                dropped, self._dropped_notices = self._dropped_notices, 0
        if dropped:
            self._emit_activity("", "表示が追いつかず %d 件の通知を省略しました" % dropped)

    def _emit_started(self, ip, filename, total, direction, path=None, ftp_path=None):
        """開始を通知し、この転送の表示名を返す（束ねたときは既存の行の表示名）。

        接続側はこれを覚えて、後の進捗・完了・中断に display として渡す。
        束ねた接続の片方が先に終わると台帳の鍵が外れるので、残った接続の
        通知を basename に戻すと、別フォルダの同名ファイルの行へ付いてしまう
        """
        key = (ip, path or filename, direction)
        if key in self._tx:
            return self._tx[key]  # 既に行がある（機器の複数接続を1行に束ねる）
        # パネルは表示名で行を追跡する。同じ相手・同じ方向で別の場所の同名
        # ファイルが転送中なら、取り違えないようルートからのパスで見せる
        name = filename
        if ftp_path and name in [v for (i, _, d), v in list(self._tx.items())
                                 if i == ip and d == direction]:
            name = ftp_path
        self._tx[key] = name
        shown = self._take_notice()
        self._tx_row[key] = "shown" if shown else "hidden"
        if shown:
            self.transfer_started.emit(ip, name, int(total), direction)
        return name

    # 以下の display は、その接続が開始時に受け取った表示名（無ければ None）。
    # 台帳に鍵が無いときは basename より先にこれを使う
    def _emit_progress(self, ip, filename, done, total, direction, path=None,
                       display=None):
        key = (ip, path or filename, direction)
        name = self._tx.get(key, display or filename)
        # 開始を省いた転送の進捗は出さない（行が片側だけになる）。進捗は
        # 次の進捗か完了で置き換わるので、省略件数には数えない
        if self._tx_row.get(key) == "hidden":
            return
        if self._take_notice(count_drop=False):
            self.transfer_progress.emit(ip, name, int(done), int(total), direction)

    def _emit_complete(self, ip, filename, done, total, direction, path=None,
                       display=None):
        # 完了で解放し次の転送は新規行に
        key = (ip, path or filename, direction)
        name = self._tx.pop(key, display or filename)
        if self._take_closing_notice(self._tx_row.pop(key, None)):
            self.transfer_complete.emit(ip, name, int(done), int(total), direction)

    def _emit_interrupted(self, ip, filename, direction, path=None, display=None):
        """未完了で終わった転送（ABOR・接続断・停止）を通知する。

        完了と同じく鍵を解放する。残したままにすると、進行中の表示が
        そのまま残り、同じファイルの再試行が開始として通知されない。
        """
        key = (ip, path or filename, direction)
        name = self._tx.pop(key, display or filename)
        if self._take_closing_notice(self._tx_row.pop(key, None)):
            self.transfer_interrupted.emit(ip, name, direction)

    # 匿名に与える権限。認証ユーザー用の "elradfmwMT" を使い回すと、
    # 資格情報なしでルート配下を上書き・削除・改名・フォルダ作成できる。
    # このパネルの主用途は `copy running-config ftp://…`、つまり機器が
    # コンフィグを置きに来る経路なので、書き込みを許す場合もそれに要る
    # 分だけにする（d 削除 / f 改名 / m フォルダ作成 / M chmod / T chmtime は不要）。
    ANON_PERM_READ = "elr"          # cd / 一覧 / 取得
    ANON_PERM_WRITE = "elrw"        # + 置く

    def start(self, port=21, root_dir="./ftp_root", username="",
              password="", anonymous=False, passive_ports=(50100, 50150),
              anonymous_write=False):
        if self.is_running:
            self.error_occurred.emit("サーバーは既に実行中です")
            return False
        # 前回の stop() で待受スレッドが抜けきらなかったときは、ソケットを
        # 閉じるのもそのスレッドなのでポートはまだ掴まれている。bind して
        # 原因の分からない失敗にする前に、もう一度だけ待つ
        if not self._await_previous_thread():
            self.error_occurred.emit(PREVIOUS_STOP_INCOMPLETE_MESSAGE)
            return False
        # 復号に失敗した値は "DPAPI:..." の暗号文のまま設定から渡ってくる。
        # それを認証パスワードとして登録すると、暗号文でログインできる一方で
        # 本来のパスワードは 530 になり、利用者には原因が分からない。
        # 判定は下の add_user と同じ条件（username と password が両方非空）に
        # 合わせる。匿名専用の構成ではパスワードは誰の資格情報にもならないので、
        # 古い暗号文が残っているだけで起動を断ると回帰になる
        if username and PasswordCrypto().is_encrypted(password):
            self.error_occurred.emit(UNDECRYPTABLE_PASSWORD_MESSAGE)
            return False
        import os
        os.makedirs(root_dir, exist_ok=True)
        # ファイアウォールは自動設定しない（3CDaemon 方式）。管理者昇格(UAC)を避けるため、
        # 制御21/passive の受信許可は Windows 標準の初回プロンプト／既存ルールに委ねる。
        self._emit_activity("", "ファイアウォール: 自動設定なし（Windowsの許可に委ねます）")

        try:
            authorizer = DummyAuthorizer()
            if username and password:
                # pyftpdlib はユーザー名 "anonymous" を特別扱いし、パスワードの
                # 照合を省略する（2.2.0 は厳密一致。大文字小文字の違いも紛らわしい
                # ので保守的に断る）。通常ユーザーとして登録すると「匿名を許可
                # しない」設定でも任意のパスワードで全権限が通ってしまう。
                # パスワードが空なら add_user は呼ばれず、権限を絞った匿名の
                # 経路だけが動くので、その組み合わせは断らない
                if username.strip().lower() == "anonymous":
                    self.error_occurred.emit(
                        "ユーザー名 anonymous は通常ユーザーとして登録できません"
                        "（パスワードが照合されません）。匿名を使う場合は「匿名を許可」を"
                        "有効にし、ユーザー名とパスワードは空にしてください")
                    return False
                authorizer.add_user(username, password, root_dir, perm="elradfmwMT")
            if anonymous:
                authorizer.add_anonymous(
                    root_dir,
                    perm=(self.ANON_PERM_WRITE if anonymous_write
                          else self.ANON_PERM_READ))
            if not authorizer.has_user(username) and not authorizer.has_user("anonymous"):
                self.error_occurred.emit("ユーザー名/パスワードを入力するか、匿名を許可してください")
                return False
        except ValueError as e:
            self.error_occurred.emit("FTP認証設定エラー: %s" % e)
            return False
        mgr = self

        class _ProgressDTP(DTPHandler):
            """データチャネルの送受信ごとに進捗を発火（TFTPと同じ見た目にするため）。"""
            def send(self, data):
                result = super().send(data)
                try: self.cmd_channel._emit_tx_progress(self.get_transmitted_bytes())
                except Exception: pass
                return result
            def handle_read(self):
                super().handle_read()
                try: self.cmd_channel._emit_tx_progress(self.get_transmitted_bytes())
                except Exception: pass
            # ioloop が呼ぶのは handle_read_event。DTPHandler はクラス定義時に
            # handle_read_event = handle_read と別名を束縛しているので、上書き
            # した handle_read にも付け直さないと呼ばれず、STOR の進捗が出ない
            handle_read_event = handle_read

        class _Handler(FTPHandler):
            dtp_handler = _ProgressDTP

            def ftp_RETR(self, file):
                result = super().ftp_RETR(file)  # 成功時はftpパスを返す
                if result is not None:
                    try: total = self.fs.getsize(file)
                    except Exception: total = 0
                    self._tx_name = os.path.basename(file); self._tx_total = int(total)
                    self._tx_dir = "download"; self._tx_last = 0.0; self._tx_path = file
                    self._tx_display = mgr._emit_started(
                        self.remote_ip, self._tx_name, self._tx_total, "download",
                        file, self.fs.fs2ftp(file))
                return result

            def ftp_STOR(self, file, mode="w"):
                result = super().ftp_STOR(file, mode)
                if result is not None:
                    self._tx_name = os.path.basename(file); self._tx_total = 0  # アップロードは総サイズ不明
                    self._tx_dir = "upload"; self._tx_last = 0.0; self._tx_path = file
                    self._tx_display = mgr._emit_started(
                        self.remote_ip, self._tx_name, 0, "upload",
                        file, self.fs.fs2ftp(file))
                return result

            def _display_for(self, file):
                """file の転送について開始時に受け取った表示名。別の転送なら None"""
                if file is not None and getattr(self, "_tx_path", None) == file:
                    return getattr(self, "_tx_display", None)
                return None

            def _emit_tx_progress(self, done):
                name = getattr(self, "_tx_name", None)
                if not name: return
                now = time.monotonic()
                if now - getattr(self, "_tx_last", 0.0) < 0.2: return  # 約200msに間引き
                self._tx_last = now
                path = getattr(self, "_tx_path", None)
                mgr._emit_progress(self.remote_ip, name, int(done),
                                   int(getattr(self, "_tx_total", 0)), getattr(self, "_tx_dir", "download"),
                                   path, self._display_for(path))

            def _forget_tx(self):
                """終わった転送を制御接続から降ろす。

                進捗はデータチャネルの送受信ごとに出るが、ftp_STOR を
                通らないデータ転送（名前をサーバが決める STOU、一覧の
                LIST / NLST）もある。消さずに残すと、それらの進捗が
                直前に終わった転送の名前・方向で出てしまう
                """
                self._tx_name = None; self._tx_path = None

            def on_file_sent(self, file):
                try: total = os.path.getsize(file)
                except OSError: total = 0
                mgr._emit_complete(self.remote_ip, os.path.basename(file), total, total, "download",
                                   file, self._display_for(file))
                self._forget_tx()
            def on_file_received(self, file):
                try: total = os.path.getsize(file)
                except OSError: total = 0
                mgr._emit_complete(self.remote_ip, os.path.basename(file), total, total, "upload",
                                   file, self._display_for(file))
                self._forget_tx()
            # 未完了で終わったとき（ABOR・データ接続の切断・サーバ停止）。
            # pyftpdlib が DTP を閉じる際に必ずどちらかを呼ぶ
            def on_incomplete_file_sent(self, file):
                mgr._emit_interrupted(self.remote_ip, os.path.basename(file), "download",
                                      file, self._display_for(file))
                self._forget_tx()
            def on_incomplete_file_received(self, file):
                mgr._emit_interrupted(self.remote_ip, os.path.basename(file), "upload",
                                      file, self._display_for(file))
                self._forget_tx()
            def on_connect(self):
                mgr._emit_activity(self.remote_ip, "接続")
            def on_disconnect(self):
                mgr._emit_activity(self.remote_ip, "切断")

        _Handler.authorizer = authorizer
        _Handler.passive_ports = range(passive_ports[0], passive_ports[1] + 1)
        try:
            # pyftpdlib は is_logging_configured()==False だと config_logging() を呼び、
            # logging._srcfile/logThreads/logProcesses/logMultiprocessing 等プロセス全体の
            # 状態を書き換えてしまう。"pyftpdlib" ロガーに handler を付けて configured 済み
            # とみなさせ、その変異を回避する。level=WARNING で接続/コマンド単位のINFOスパムも抑止
            # （propagate はデフォルトTrueのままなので WARNING 以上はアプリのログへ届く）。
            import logging
            _pyftpd_logger = logging.getLogger("pyftpdlib")
            _pyftpd_logger.setLevel(logging.WARNING)
            if not _pyftpd_logger.handlers:
                _pyftpd_logger.addHandler(logging.NullHandler())
            # ioloop を渡さないと、pyftpdlib はプロセス全体で 1 つの共有
            # インスタンスを使う。共有すると、片方を停止したときの
            # close_all() がもう片方の待ち受けソケットまで閉じる
            # （実測で、動いている側へログインできなくなった）。
            # 2 本の待受スレッドが同じ fd の一覧を同時に読み書きする
            # 状態も、pyftpdlib が想定していない
            self._server = _PyFTPServer(("0.0.0.0", port), _Handler,
                                        ioloop=_PyIOLoop.factory())
            self.port = self._server.address[1]
        except Exception as e:
            self.error_occurred.emit("FTP起動失敗: %s" % e)
            self._server = None
            return False
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, args=(self._server, self._stop_event),
            daemon=True)
        self._thread.start()
        self.is_running = True
        self.started.emit()
        return True

    # ioloop を 1 周させる間隔。停止要求に気づくまでの遅れの上限でもある
    POLL_INTERVAL_SECONDS = 0.2

    # 待受スレッドの終了を待つ上限。停止要求は 1 周ごとに見るので、
    # 通常は POLL_INTERVAL_SECONDS 以内に抜ける
    STOP_TIMEOUT_SECONDS = 3.0

    def _serve(self, server, stop_event):
        """待受スレッド本体。ソケットを閉じるのもこのスレッドで行う。

        serve_forever() に任せると、停止は別スレッド（GUI スレッド）から
        close_all() を呼ぶしかない。ioloop を回しているのとは別のスレッドが
        その ioloop の socket_map と fd の一覧を書き換えることになり、
        pyftpdlib はそれを想定していない（同じ ioloop に対する登録・解除は
        すべて poll しているスレッドから行われる前提で書かれている）。

        そこで ioloop は 1 周ずつ自分で回し、停止要求に気づいたら
        このスレッドから閉じる。停止までの遅れは 1 周ぶんで済む。

        補足: これを入れた時点では、全体テストを落としていた access
        violation の原因がここだと考えていた。実際の原因は別（シグナルの
        中継に .emit を渡していたこと。tests/
        test_signal_relay_outlives_receiver.py を参照）で、select 実行中に
        別スレッドがその list を縮めても Python 3.12 / Windows 11 では
        落ちないことが後の実測で確かめられている。この形自体は
        pyftpdlib の前提に沿っていて害が無いので残している。
        """
        try:
            while not stop_event.is_set():
                if server.ioloop.socket_map:
                    server.ioloop.loop(self.POLL_INTERVAL_SECONDS,
                                       blocking=False)
                else:
                    # 待受ソケットまで閉じられた（外から close_all された
                    # など）。空回りせずに停止要求を待つ
                    stop_event.wait(self.POLL_INTERVAL_SECONDS)
        finally:
            try:
                server.close_all()
            except Exception:
                pass

    def _await_previous_thread(self):
        """前回の停止で抜けきらなかった待受スレッドを待ち直す。

        停止できたかを返す。閉じる役目が待受スレッド側にあるため、join が
        上限で諦めた時点ではポートがまだ解放されていない。stop() は参照を
        残すので、ここでもう一度だけ待ってから起動の可否を決める。
        """
        thread, self._thread = self._thread, None
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout=self.STOP_TIMEOUT_SECONDS)
        if thread.is_alive():
            self._thread = thread   # 次の機会にまた待てるよう捨てない
            return False
        return True

    def stop(self):
        thread, self._thread = self._thread, None
        self._stop_event.set()
        self._server = None
        # スレッドが抜けるまで待ってから戻る。待たずに戻ると、直後に
        # このマネージャ（QObject）が破棄されたとき、生き残ったスレッド
        # からの emit が解放済みオブジェクトへ届いてプロセスごと落ちる
        # （停止直後にパネルやアプリを閉じる操作で起こる）。ソケットを
        # 閉じるのもこの join のあいだにスレッド側で終わる
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.STOP_TIMEOUT_SECONDS)
            if thread.is_alive():
                # 上限で諦めた＝ソケットはまだ閉じられていない。参照を残し、
                # 次の start() が bind する前に待ち直せるようにする
                self._thread = thread
        self.is_running = False
        self._tx.clear()
        self._tx_row.clear()
        self.stopped.emit()

    def fix_firewall(self, port=21, passive_ports=(50100, 50150)):
        """手動: Windows FW 受信許可を追加（制御/passive/自exe、管理者昇格/UAC）。
        3CDaemon 方式で通らない環境の復旧用。押した時だけ昇格する。"""
        try:
            from .firewall import (combine_results, ensure_inbound_allow,
                                   ensure_self_program_allow)
            ok, msg = ensure_inbound_allow("FTP Server", "TCP", port)
            self._emit_activity("", "ファイアウォール(制御): %s" % msg)
            lo, hi = passive_ports
            ok2, msg2 = ensure_inbound_allow("FTP Passive", "TCP", "%d-%d" % (lo, hi))
            self._emit_activity("", "ファイアウォール(passive): %s" % msg2)
            ok3, msg3 = ensure_self_program_allow()
            self._emit_activity("", "ファイアウォール(自exe): %s" % msg3)
            return combine_results([(ok, msg), (ok2, msg2), (ok3, msg3)])
        except Exception as e:
            self.error_occurred.emit("ファイアウォール設定エラー: %s" % e)
            return False, str(e)
