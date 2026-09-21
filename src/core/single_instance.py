"""多重起動を断る（2 つ目は起動せず、動いている方のウィンドウを前へ出す）

NetBelt を 2 つ起動すると、どちらの ConfigManager も自分が読み込んだ内容を
config.json へ丸ごと書き戻すため、あとから保存した方が相手の追加した機器・
パスワード・グループ・マクロを警告もバックアップも無しに消す（実測）。
known_hosts にはプロセスをまたぐ錠があるが、config.json には無い。

止め方は PyQt6 の QLocalServer / QLocalSocket だけで組む（依存は足さない）。
先に名前を取った方が本体で、2 つ目はその名前へ繋げた時点で「既に動いて
いる」と分かるので、繋いで切るだけで終了する。本体側はその接続を合図に
ウィンドウを前へ出す。
"""
import getpass
import hashlib

from PyQt6.QtCore import QObject, Qt
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


class SingleInstanceGuard(QObject):
    """起動が 1 つだけであることを保つ。

    使い方は main() を参照。another_instance_is_running() が True なら何も
    作らずに終わり、False なら listen() で名前を取ってから set_window() で
    前へ出すウィンドウを渡す。
    """

    # 名前へ繋がるかを待つ上限（ミリ秒）。動いていなければすぐ失敗するので、
    # 起動を目に見えて遅らせない長さにする
    CONNECT_TIMEOUT_MS = 500

    def __init__(self, name: str = None, parent=None):
        super().__init__(parent)
        self._name = name or default_server_name()
        self._server = None
        self._window = None
        # 窓を覚える前に届いた「前へ出せ」の合図。MainWindow を作る途中で
        # モーダル（設定ファイルの警告など）が開くと、そのモーダルが回す
        # 入れ子のイベントループで 2 つ目の起動の接続が処理されてしまい、
        # 窓がまだ無いまま raise_window() が呼ばれる。捨てると利用者には
        # 「2 回目のダブルクリックで何も起きない」ように見える
        self._pending_raise = False

    def another_instance_is_running(self) -> bool:
        """既に動いている NetBelt があれば True（その窓を前へ出させる）"""
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
        """名前を手放す。次の起動が断られないようにする"""
        server, self._server = self._server, None
        if server is None:
            return
        try:
            server.close()
            QLocalServer.removeServer(self._name)
        except Exception as e:
            print(f"[WARNING] 多重起動の防止を解除できませんでした: {e}")
