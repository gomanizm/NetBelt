"""FTP サーバー（pyftpdlib ラッパ）。UI 通知は Qt シグナル。"""
import threading
import time

from PyQt6.QtCore import QObject, pyqtSignal
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler, DTPHandler
from pyftpdlib.servers import FTPServer as _PyFTPServer


class FTPServerManager(QObject):
    started = pyqtSignal()
    stopped = pyqtSignal()
    error_occurred = pyqtSignal(str)
    client_activity = pyqtSignal(str, str)      # ip, message
    transfer_started = pyqtSignal(str, str, int, str)        # ip, filename, total, direction
    transfer_progress = pyqtSignal(str, str, int, int, str)  # ip, filename, done, total, direction
    transfer_complete = pyqtSignal(str, str, int, int, str)  # ip, filename, done, total, direction

    def __init__(self, parent=None):
        super().__init__(parent)
        self._server = None
        self._thread = None
        self.is_running = False
        self.port = 0
        # (ip, filename, direction) 単位の表示コアレス。機器が1回の copy で複数FTP接続を張っても
        # 論理転送1本=履歴1行にする（Cisco IOS は本転送前に接続→RETR→即切断のプローブを複数回行う）。
        self._tx = {}

    def _emit_started(self, ip, filename, total, direction):
        key = (ip, filename, direction)
        if key in self._tx:
            return  # 既に行がある（機器の複数接続を1行に束ねる）
        self._tx[key] = True
        self.transfer_started.emit(ip, filename, int(total), direction)

    def _emit_progress(self, ip, filename, done, total, direction):
        self.transfer_progress.emit(ip, filename, int(done), int(total), direction)

    def _emit_complete(self, ip, filename, done, total, direction):
        self._tx.pop((ip, filename, direction), None)  # 完了で解放し次の転送は新規行に
        self.transfer_complete.emit(ip, filename, int(done), int(total), direction)

    def start(self, port=21, root_dir="./ftp_root", username="",
              password="", anonymous=False, passive_ports=(50100, 50150)):
        if self.is_running:
            self.error_occurred.emit("サーバーは既に実行中です")
            return False
        import os
        os.makedirs(root_dir, exist_ok=True)
        # ファイアウォールは自動設定しない（3CDaemon 方式）。管理者昇格(UAC)を避けるため、
        # 制御21/passive の受信許可は Windows 標準の初回プロンプト／既存ルールに委ねる。
        self.client_activity.emit("", "ファイアウォール: 自動設定なし（Windowsの許可に委ねます）")

        try:
            authorizer = DummyAuthorizer()
            if username and password:
                authorizer.add_user(username, password, root_dir, perm="elradfmwMT")
            if anonymous:
                authorizer.add_anonymous(root_dir, perm="elradfmwMT")
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

        class _Handler(FTPHandler):
            dtp_handler = _ProgressDTP

            def ftp_RETR(self, file):
                result = super().ftp_RETR(file)  # 成功時はftpパスを返す
                if result is not None:
                    try: total = self.fs.getsize(file)
                    except Exception: total = 0
                    self._tx_name = os.path.basename(file); self._tx_total = int(total)
                    self._tx_dir = "download"; self._tx_last = 0.0
                    mgr._emit_started(self.remote_ip, self._tx_name, self._tx_total, "download")
                return result

            def ftp_STOR(self, file, mode="w"):
                result = super().ftp_STOR(file, mode)
                if result is not None:
                    self._tx_name = os.path.basename(file); self._tx_total = 0  # アップロードは総サイズ不明
                    self._tx_dir = "upload"; self._tx_last = 0.0
                    mgr._emit_started(self.remote_ip, self._tx_name, 0, "upload")
                return result

            def _emit_tx_progress(self, done):
                name = getattr(self, "_tx_name", None)
                if not name: return
                now = time.monotonic()
                if now - getattr(self, "_tx_last", 0.0) < 0.2: return  # 約200msに間引き
                self._tx_last = now
                mgr._emit_progress(self.remote_ip, name, int(done),
                                   int(getattr(self, "_tx_total", 0)), getattr(self, "_tx_dir", "download"))

            def on_file_sent(self, file):
                try: total = os.path.getsize(file)
                except OSError: total = 0
                mgr._emit_complete(self.remote_ip, os.path.basename(file), total, total, "download")
            def on_file_received(self, file):
                try: total = os.path.getsize(file)
                except OSError: total = 0
                mgr._emit_complete(self.remote_ip, os.path.basename(file), total, total, "upload")
            def on_connect(self):
                mgr.client_activity.emit(self.remote_ip, "接続")
            def on_disconnect(self):
                mgr.client_activity.emit(self.remote_ip, "切断")

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
            self._server = _PyFTPServer(("0.0.0.0", port), _Handler)
            self.port = self._server.address[1]
        except Exception as e:
            self.error_occurred.emit("FTP起動失敗: %s" % e)
            self._server = None
            return False
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.is_running = True
        self.started.emit()
        return True

    def stop(self):
        if self._server:
            try:
                self._server.close_all()
            except Exception:
                pass
            self._server = None
        self.is_running = False
        self._tx.clear()
        self.stopped.emit()

    def fix_firewall(self, port=21, passive_ports=(50100, 50150)):
        """手動: Windows FW 受信許可を追加（制御/passive/自exe、管理者昇格/UAC）。
        3CDaemon 方式で通らない環境の復旧用。押した時だけ昇格する。"""
        try:
            from .firewall import ensure_inbound_allow, ensure_self_program_allow
            ok, msg = ensure_inbound_allow("FTP Server", "TCP", port)
            self.client_activity.emit("", "ファイアウォール(制御): %s" % msg)
            lo, hi = passive_ports
            ok2, msg2 = ensure_inbound_allow("FTP Passive", "TCP", "%d-%d" % (lo, hi))
            self.client_activity.emit("", "ファイアウォール(passive): %s" % msg2)
            ok3, msg3 = ensure_self_program_allow()
            self.client_activity.emit("", "ファイアウォール(自exe): %s" % msg3)
            return (ok and ok2 and ok3), msg
        except Exception as e:
            self.error_occurred.emit("ファイアウォール設定エラー: %s" % e)
            return False, str(e)
