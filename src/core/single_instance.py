"""多重起動を断る（2 つ目は起動せず、動いている方のウィンドウを前へ出す）

NetBelt を 2 つ起動すると、どちらの ConfigManager も自分が読み込んだ内容を
config.json へ丸ごと書き戻すため、あとから保存した方が相手の追加した機器・
パスワード・グループ・マクロを警告もバックアップも無しに消す（実測）。
known_hosts にはプロセスをまたぐ錠があるが、config.json には無い。

止め方は PyQt6 だけで組む（依存は足さない）。どちらが本体かは QLockFile の
錠で不可分に決める。錠を取れた方が本体で、取れなかった方は起動せず、
本体の名前（QLocalServer）へ繋いで切るだけで終わる。本体側はその接続を
合図にウィンドウを前へ出す。錠が取れても、名前へ繋がるなら起動しない
（錠を知らない版が動いている場合）。

錠が要るのは、QLocalServer.listen() が Windows では排他にならないため。
同じ名前で 2 つ目の名前付きパイプが作れてしまうので（実測）、「名前へ
繋がるか」を見てから listen する 2 操作では、2 つのプロセスがどちらも
相手の待受開始より前に確認を終えたときに両方とも本体になる。競合が成立
する幅は約 3 ミリ秒で、.bat の start 2 回・スタートアップ登録と手動起動の
重なりのような「ほぼ同時起動」では確実に起きる（実測 30/30）。
"""
import getpass
import hashlib
import os
import tempfile
import time

from PyQt6.QtCore import QLockFile, QObject, Qt
from PyQt6.QtNetwork import QLocalServer, QLocalSocket


def default_server_name(base: str = "NetBelt") -> str:
    """利用者ごとに分けた名前を返す。

    Windows の名前付きパイプは利用者をまたいで共有されるので、名前をそのまま
    使うと別の利用者のセッションまで「もう動いている」と断ってしまう。
    利用者名はそのまま載せず、掛け合わせた値の頭だけを使う。
    """
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    digest = hashlib.sha256(user.encode("utf-8", "replace")).hexdigest()
    return "%s-%s" % (base, digest[:16])


def default_lock_path(name: str) -> str:
    """錠ファイルの置き場所を返す（名前ごとに分かれる）。

    一時ディレクトリは利用者ごとに分かれており、名前自体にも利用者を
    掛け合わせた値が入っているので、別の利用者のセッションとはぶつからない。
    """
    return os.path.join(tempfile.gettempdir(), "%s.lock" % name)


class SingleInstanceGuard(QObject):
    """起動が 1 つだけであることを保つ。

    使い方は main() を参照。another_instance_is_running() が True なら何も
    作らずに終わり、False なら listen() で名前を取ってから set_window() で
    前へ出すウィンドウを渡す。

    another_instance_is_running() は確認だけでなく、起動してよい権利（錠）を
    その場で取る。確認と取得を分けると、その隙間に相手が入り込めてしまう。
    取った錠は close() で手放す。
    """

    # 名前へ繋がるかを待つ上限（ミリ秒）。動いていなければすぐ失敗するので、
    # 起動を目に見えて遅らせない長さにする
    CONNECT_TIMEOUT_MS = 500
    # 錠を取れなかった側が、動いている方へ合図を送るのを諦めるまで
    # （ミリ秒）と、送り直す間隔。錠を取った方がまだ listen() まで来て
    # いないことがあり、1 回きりだと利用者には「2 回目のダブルクリックで
    # 何も起きない」ように見える
    RAISE_RETRY_MS = 3000
    RAISE_RETRY_INTERVAL_MS = 20

    def __init__(self, name: str = None, parent=None):
        super().__init__(parent)
        self._name = name or default_server_name()
        self._server = None
        self._window = None
        # 自分が持っている錠（持っていなければ None）
        self._lock = None
        # 錠そのものを使えない環境（書けないディレクトリなど）かどうか
        self._lock_unusable = False
        # 窓を覚える前に届いた「前へ出せ」の合図。MainWindow を作る途中で
        # モーダル（設定ファイルの警告など）が開くと、そのモーダルが回す
        # 入れ子のイベントループで 2 つ目の起動の接続が処理されてしまい、
        # 窓がまだ無いまま raise_window() が呼ばれる。捨てると利用者には
        # 「2 回目のダブルクリックで何も起きない」ように見える
        self._pending_raise = False

    def _take_lock(self) -> bool:
        """起動してよい権利を不可分に取る（取れたら True）。

        取れなかった理由が「相手が持っている」以外（錠を作れないなど）の
        ときは、錠を使わない扱いにして True を返す。断る方の失敗で起動
        できなくする方が影響が大きい（listen() が失敗したときと同じ考え方）。
        """
        if self._lock is not None or self._lock_unusable:
            return True
        try:
            lock = QLockFile(default_lock_path(self._name))
            # 既定の 30 秒で勝手に失効すると、長く動かしている方の錠が
            # 無効になって 2 つ目が開いてしまう。自動失効は切る。異常終了で
            # 残った錠は、持ち主のプロセスが居なくなった時点で取り直せる
            # （実測: 保持プロセスを kill したあとの tryLock(0) は True）
            lock.setStaleLockTime(0)
            if lock.tryLock(0):
                self._lock = lock
                return True
            if lock.error() == QLockFile.LockError.LockFailedError:
                return False
            print("[WARNING] 多重起動の確認に失敗しました: %s" % lock.error())
        except Exception as e:
            print(f"[WARNING] 多重起動の確認に失敗しました: {e}")
        self._lock_unusable = True
        return True

    def _wake_running_instance(self) -> bool:
        """動いている方へ「窓を前へ出せ」と伝える（届いたら True）。

        錠を取った方が listen() へ着くまでには間があるので、少しの間
        送り直す。
        """
        deadline = time.monotonic() + self.RAISE_RETRY_MS / 1000.0
        while True:
            if self._connect_once():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.RAISE_RETRY_INTERVAL_MS / 1000.0)

    def another_instance_is_running(self) -> bool:
        """既に動いている NetBelt があれば True（その窓を前へ出させる）

        確認と同時に、自分が本体になる権利（錠）を取る。False を返した
        ときは錠を持っている（錠を使えない環境でだけ、持たないまま False）
        ので、そのまま listen() へ進んでよい。
        """
        if not self._take_lock():
            # 相手が錠を持っている＝向こうが本体。窓を前へ出してもらう
            self._wake_running_instance()
            return True
        # 錠は取れた。ただし錠を知らない版（この変更より前の NetBelt）が
        # 動いていると錠は空いたままなので、名前へ繋がるかも今までどおり
        # 見る。錠を使えない環境で判断できるのも、こちらだけ
        if self._connect_once():
            # 自分は本体にならない。取った錠はここで返す
            self.close()
            return True
        return False

    def _connect_once(self) -> bool:
        """名前へ繋がれば True（繋がったこと自体が「前へ出せ」の合図）"""
        socket = QLocalSocket(self)
        try:
            socket.connectToServer(self._name)
            if not socket.waitForConnected(self.CONNECT_TIMEOUT_MS):
                return False
            # 繋がったこと自体が合図。本体はこれを受けて窓を前へ出す
            socket.disconnectFromServer()
            return True
        except Exception as e:
            # 断れなかっただけで起動を止めない（止める方が影響が大きい）
            print(f"[WARNING] 多重起動の確認に失敗しました: {e}")
            return False
        finally:
            socket.close()
            socket.deleteLater()

    def listen(self) -> bool:
        """自分を「動いている NetBelt」として登録する（取れたら True）"""
        # 名前は排他にならないので、錠を持っていなければここで取る。
        # 通常は another_instance_is_running() が取り終えている
        self._take_lock()
        try:
            # 異常終了のあとに残った印で起動できなくならないよう、先に消す。
            # Windows の名前付きパイプはプロセスと一緒に消えるが、UNIX
            # ドメインソケットはファイルが残る
            QLocalServer.removeServer(self._name)
            server = QLocalServer(self)
            if not server.listen(self._name):
                print("[WARNING] 多重起動の防止を有効にできませんでした: %s"
                      % server.errorString())
                server.deleteLater()
                return False
            server.newConnection.connect(self._on_new_connection)
            self._server = server
            return True
        except Exception as e:
            print(f"[WARNING] 多重起動の防止を有効にできませんでした: {e}")
            return False

    def set_window(self, window) -> None:
        """2 つ目の起動があったときに前へ出すウィンドウを覚える。

        覚える前に合図が届いていたら、この時点で前へ出す（合図は 1 回で
        使い切る）。
        """
        self._window = window
        if window is not None and self._pending_raise:
            self._pending_raise = False
            self.raise_window()

    def raise_window(self) -> None:
        """覚えているウィンドウを前へ出す（最小化されていれば戻す）"""
        window = self._window
        if window is None:
            # まだ窓が無い。合図を取っておき、set_window() で前へ出す
            self._pending_raise = True
            return
        state = window.windowState()
        if state & Qt.WindowState.WindowMinimized:
            window.setWindowState((state & ~Qt.WindowState.WindowMinimized)
                                  | Qt.WindowState.WindowActive)
        window.show()
        window.raise_()
        window.activateWindow()

    def _on_new_connection(self) -> None:
        """2 つ目の起動が繋いできた。合図として受け、窓を前へ出す"""
        server = self._server
        if server is None:
            return
        while server.hasPendingConnections():
            connection = server.nextPendingConnection()
            if connection is not None:
                connection.close()
                connection.deleteLater()
        self.raise_window()

    def close(self) -> None:
        """名前と錠を手放す。次の起動が断られないようにする"""
        server, self._server = self._server, None
        lock, self._lock = self._lock, None
        if server is not None:
            try:
                server.close()
                QLocalServer.removeServer(self._name)
            except Exception as e:
                print(f"[WARNING] 多重起動の防止を解除できませんでした: {e}")
        if lock is not None:
            try:
                lock.unlock()
            except Exception as e:
                print(f"[WARNING] 多重起動の防止を解除できませんでした: {e}")
