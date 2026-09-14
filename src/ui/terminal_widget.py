from PyQt6.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QTabWidget, QMenu
from PyQt6.QtGui import (QFont, QColor, QPalette, QKeyEvent, QAction,
                         QTextCursor, QTextCharFormat)
from PyQt6.QtCore import Qt, pyqtSignal
from typing import Dict, Optional

from core.terminal import parser as vt
from core.terminal.attrs import DEFAULT
from core.terminal.screen import Screen, BLANK

# SGR の基本 16 色 (xterm の既定値)。0-7 が基本、8-15 が明色
def _u16(text: str) -> int:
    """文書上の長さ（UTF-16 のコード単位）を返す。

    QTextDocument の位置は UTF-16 のコード単位で数える。Python の len()
    はコードポイント数なので、BMP 外の文字（絵文字、CJK 拡張B など）が
    1つ画面に出るたびに1つずつずれる。ずれた添字を setPosition へ渡すと、
    書き換え範囲が本来より手前を指し、改行の手前へ文字を挿し込んだり
    隣の文字を巻き込んで消したりする。位置を数えるときは必ずこれを使う。
    """
    return len(text.encode("utf-16-le")) // 2


ANSI_COLOURS = (
    "#000000", "#cd0000", "#00cd00", "#cdcd00",
    "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
    "#7f7f7f", "#ff0000", "#00ff00", "#ffff00",
    "#5c5cff", "#ff00ff", "#00ffff", "#ffffff")


class InteractiveTerminal(QTextEdit):
    """キー入力を処理できるインタラクティブなターミナル"""
    
    key_pressed = pyqtSignal(str)  # キー入力シグナル
    reconnect_requested = pyqtSignal()  # 再接続要求シグナル
    macro_execute_requested = pyqtSignal(str)  # マクロ実行要求シグナル（マクロ名）
    macro_settings_requested = pyqtSignal()  # マクロ設定画面要求シグナル
    macro_stop_requested = pyqtSignal()  # 実行中のマクロ（コマンドリスト）停止要求シグナル
    keepalive_start_requested = pyqtSignal()  # キープアライブ開始要求シグナル
    keepalive_stop_requested = pyqtSignal()  # キープアライブ停止要求シグナル
    # Ctrl+ホイールでのフォントサイズ変更要求（回した向き: +1 / -1）
    font_size_change_requested = pyqtSignal(int)
    # ウィジェットの大きさが変わった（行数・桁数の再計算が要る）
    resized = pyqtSignal()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(False)
        self._input_enabled = False
        self._reconnect_mode = False  # 再接続待機モード
        self._is_recording = False  # ログ記録中フラグ
        self._macro_list = []  # 利用可能なマクロリスト
        self._keepalive_active = False  # キープアライブ動作中フラグ
        self._command_list_active = False  # マクロ（コマンドリスト）実行中フラグ
        # まだ送り切っていない貼り付け。まとめて送ると GUI が止まるので、
        # 区切りごとにイベントループへ譲りながら流す
        self._send_queue = []
        self._sending = False
    
    def set_keepalive_status(self, active: bool):
        """キープアライブの状態を設定"""
        self._keepalive_active = active

    def set_command_list_status(self, active: bool):
        """マクロ（コマンドリスト）が実行中かを設定"""
        self._command_list_active = active
    
    def set_input_enabled(self, enabled: bool):
        """入力の有効/無効を切り替え"""
        self._input_enabled = enabled
    
    def set_reconnect_mode(self, enabled: bool):
        """再接続モードを切り替え"""
        self._reconnect_mode = enabled
        if enabled:
            self._input_enabled = True  # 再接続モードではEnterキーを受け付ける
            # 送りかけの貼り付けは、切れた接続宛てのもの。残しておくと
            # 再接続で同じタブを使い回すため、新しい接続へそのまま流れる
            self._send_queue.clear()
            self._sending = False
    
    def can_send_input(self) -> bool:
        """
        いま機器へ文字を送れる状態か

        再接続待機中は Enter による再接続だけを受け付ける（切断中に送信すると
        バナーが増殖するため）。keyPressEvent と同じ条件をここへ集約し、
        ペースト経路が迂回しないようにする。

        Returns:
            送信できるならTrue
        """
        return self._input_enabled and not self._reconnect_mode

    def mouseReleaseEvent(self, event):
        """マウスリリース後、カーソルを末尾に戻す"""
        # デフォルトの動作（テキスト選択）を実行
        super().mouseReleaseEvent(event)
        
        # 選択がない場合のみカーソルを末尾に移動
        from PyQt6.QtGui import QTextCursor
        cursor = self.textCursor()
        if not cursor.hasSelection():
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.setTextCursor(cursor)
    
    def send_text(self, text: str) -> bool:
        """テキストを機器へ送る（画面へは直接書かない）

        画面に出るのは機器が返してきたエコーだけにする。ここで直接
        書いてしまうと、機器が受け取っていない文字が入力済みのように
        見えてしまう。

        Returns:
            bool: 送ったなら True（送れる状態でない・空文字なら False）
        """
        if not text or not self.can_send_input():
            return False
        # 改行は端末と同じく CR で送る
        payload = text.replace('\r\n', '\r').replace('\n', '\r')
        # アプリが ESC[?2004h を送ってきていたら、貼り付けを目印で
        # 包む (xterm のブラケットペースト)。bash はこれで貼り付けを
        # 即実行せず 1 かたまりの編集として扱える
        screen = getattr(self, "_screen", None)
        if screen is not None and screen.bracketed_paste:
            payload = "\x1b[200~" + payload + "\x1b[201~"
        self._queue_send(payload)
        return True

    # 1回に送り出す文字数。大きすぎると譲る間隔が空き、小さすぎると
    # 往復が増える。設定 100 行ぶんがおよそ 2,000 文字なので、
    # その規模なら数回で終わる。
    SEND_CHUNK = 512

    def queue_macro_send(self, payload: str):
        """マクロ（コマンドリスト）の1行を送信列へ積む

        マクロ由来という印を付けておく。停止したときに、まだ送っていない
        ぶんだけを cancel_macro_sends で取り消せるようにするため。
        """
        self._queue_send(payload, from_macro=True)

    def cancel_macro_sends(self):
        """まだ送っていないマクロ由来の断片を送信列から取り除く

        停止したのに、貼り付けの排出待ちで列に残っていたコマンドが
        後から機器へ届くのを防ぐ。打鍵や貼り付けは巻き添えにしない。
        """
        self._send_queue = [
            entry for entry in self._send_queue if not entry[1]
        ]

    def _queue_send(self, payload: str, from_macro: bool = False):
        """機器へ送るものを列の末尾へ積む

        機器へ向かうものは、貼り付けも打鍵も IME の確定も問い合わせへの
        応答も、すべてここを通す。直接 key_pressed を叩くと、まだ送り
        終えていない貼り付けの残りを追い越して先に届く。1文字ずつ同期
        送信していた頃は GUI が完全に止まっていたので起こらなかったが、
        区切りごとにイベントループへ譲るようにしたことで起きるように
        なった。CLI では途中まで貼られた行が先に実行されてしまう。

        列が空なら _drain_send_queue がその場で送るので、打鍵1つの
        ために往復が増えることはない。
        """
        if not payload:
            return
        # [送る文字列, マクロ由来か] の形で積む。停止のときに由来で選り分ける
        self._send_queue.append([payload, from_macro])
        if not self._sending:
            self._drain_send_queue()

    def _drain_send_queue(self):
        """溜めた送信を、区切りごとにイベントループへ譲りながら流す。

        以前は貼り付けを 1 文字ずつ emit していた。受け口は同一スレッドの
        send_command へ直結（DirectConnection）なので、文字数ぶんの同期
        送信が GUI スレッドで回り、その間は再描画も操作も通らなかった。

        送信そのものにかかる時間は非同期にしても縮まらない。縮められるのは
        「その間 GUI が息をするか」なので、まとめて送りつつ間で譲る。

        最初のひと区切りはその場で送る。短い貼り付けの振る舞いを変えない
        ため（呼んだ直後に送信済みであることを前提にしている箇所がある）。
        """
        from PyQt6.QtCore import QTimer

        # 再接続待ちに入っていたら、予約済みの排出も含めて打ち切る。
        # 残りは切れた接続宛てなので、新しい接続へ送ってはいけない
        if self._reconnect_mode:
            self._send_queue.clear()
            self._sending = False
            return

        if not self._send_queue:
            self._sending = False
            return

        self._sending = True
        entry = self._send_queue[0]
        payload = entry[0]
        chunk, rest = payload[:self.SEND_CHUNK], payload[self.SEND_CHUNK:]
        if rest:
            entry[0] = rest
        else:
            self._send_queue.pop(0)

        self.key_pressed.emit(chunk)

        if self._send_queue:
            QTimer.singleShot(0, self._drain_send_queue)
        else:
            self._sending = False

    def custom_paste(self):
        """カスタムペースト機能 - ペーストされたテキストをSSHセッションに送信"""
        from PyQt6.QtWidgets import QApplication

        self.send_text(QApplication.clipboard().text())

    def insertFromMimeData(self, source):
        """ドロップや挿入経路を機器送信へ振り替える

        QTextEdit は編集可能なので、既定ではドロップされたテキストを
        そのまま画面へ挿入する。機器は何も受け取っていないのに
        入力済みに見えるため、送信へ回して画面へは書かない。
        """
        if source.hasText():
            self.send_text(source.text())

    def inputMethodEvent(self, event):
        """IME の入力を機器送信へ振り替える

        日本語入力の確定文字列は keyPressEvent ではなくこちらへ来る。
        受けずに既定動作へ流すと、QTextEdit が確定文字を文書へ直接挿入し、
        機器が受け取っていない文字が入力済みに見える。さらに範囲選択が
        あると選択範囲を置換するので、履歴を選んだまま確定すると機器の
        出力がその場で書き換わる。画面領域しか描き直さないため復元されず、
        文書を読む全ログ保存にも壊れたまま残る。

        変換中の未確定文字列（preedit）は機器へ送らず、画面にも出さない。
        端末の面は機器の出力を写したものなので、割り込ませると描画とずれる。

        送れる状態かどうかは can_send_input() が見る。keyPressEvent の
        早期 return はこの経路には効かないので、ここでも通す必要がある。
        貼り付けではないため、ブラケットペーストの目印では包まない。
        """
        event.accept()
        commit = event.commitString()
        if commit and self.can_send_input():
            self._queue_send(commit)


    def set_macro_list(self, macros: list):
        """
        利用可能なマクロリストを設定
        
        Args:
            macros: マクロリスト（辞書のリスト）
        """
        self._macro_list = macros
    
    def contextMenuEvent(self, event):
        """右クリックメニューをカスタマイズ（日本語化、切り取り機能を削除）"""
        menu = QMenu(self)
        
        # コピー（選択範囲がある場合のみ有効）
        copy_action = QAction("コピー", self)
        copy_action.triggered.connect(self.copy)
        copy_action.setEnabled(self.textCursor().hasSelection())
        menu.addAction(copy_action)
        
        # 貼り付け（カスタムペースト機能を使用）
        paste_action = QAction("貼り付け", self)
        paste_action.triggered.connect(self.custom_paste)
        paste_action.setEnabled(self.can_send_input())
        menu.addAction(paste_action)
        
        menu.addSeparator()
        
        # すべて選択
        select_all_action = QAction("すべて選択", self)
        select_all_action.triggered.connect(self.selectAll)
        menu.addAction(select_all_action)
        
        menu.addSeparator()
        
        # マクロメニュー
        if self._input_enabled:
            # キープアライブ
            if self._keepalive_active:
                keepalive_action = QAction("キープアライブ停止", self)
                keepalive_action.triggered.connect(lambda: self.keepalive_stop_requested.emit())
            else:
                keepalive_action = QAction("キープアライブ開始", self)
                keepalive_action.triggered.connect(lambda: self.keepalive_start_requested.emit())
            menu.addAction(keepalive_action)
            
            menu.addSeparator()
            
            # マクロ実行サブメニュー
            if self._macro_list:
                macro_menu = QMenu("マクロ実行", self)
                for macro in self._macro_list:
                    macro_name = macro.get("name", "")
                    macro_desc = macro.get("description", "")
                    
                    if macro_desc:
                        action_text = f"{macro_name} - {macro_desc}"
                    else:
                        action_text = macro_name
                    
                    macro_action = QAction(action_text, self)
                    macro_action.triggered.connect(
                        lambda checked, name=macro_name: self.macro_execute_requested.emit(name)
                    )
                    macro_menu.addAction(macro_action)
                
                menu.addMenu(macro_menu)

            # 実行中のマクロを止める。これが無いと、誤ったマクロを流し
            # 始めたときタブを閉じる以外に中断する手段が無い
            if self._command_list_active:
                macro_stop_action = QAction("マクロ停止", self)
                macro_stop_action.triggered.connect(lambda: self.macro_stop_requested.emit())
                menu.addAction(macro_stop_action)
            
            menu.addSeparator()
        
        # ログ機能メニュー
        # 1. 現在表示されている全ログの保存
        save_all_log_action = QAction("全ログ保存", self)
        save_all_log_action.triggered.connect(self._on_save_all_log)
        menu.addAction(save_all_log_action)
        
        # 2. ログ記録開始/停止（記録状態によって切り替え）
        if self._is_recording:
            stop_log_action = QAction("ログ記録停止", self)
            stop_log_action.triggered.connect(self._on_stop_log_recording)
            menu.addAction(stop_log_action)
        else:
            start_log_action = QAction("ログ記録開始", self)
            start_log_action.triggered.connect(self._on_start_log_recording)
            menu.addAction(start_log_action)
        
        # メニューを表示
        menu.exec(event.globalPos())
    
    def _on_save_all_log(self):
        """現在表示されている全ログを保存"""
        # 親ウィジェット（TerminalWidget）にシグナルを送る
        parent = self.parent()
        while parent and not isinstance(parent, TerminalWidget):
            parent = parent.parent()
        
        if parent and isinstance(parent, TerminalWidget):
            # 現在のタブのインデックスを取得
            current_widget = parent.tab_widget.currentWidget()
            if current_widget == self:
                parent.save_current_log()
    
    def _on_start_log_recording(self):
        """ログ記録を開始"""
        # 親ウィジェット（TerminalWidget）にシグナルを送る
        parent = self.parent()
        while parent and not isinstance(parent, TerminalWidget):
            parent = parent.parent()
        
        if parent and isinstance(parent, TerminalWidget):
            # 現在のタブのインデックスを取得
            current_widget = parent.tab_widget.currentWidget()
            if current_widget == self:
                parent.start_log_recording()
    
    def _on_stop_log_recording(self):
        """ログ記録を停止"""
        # 親ウィジェット（TerminalWidget）にシグナルを送る
        parent = self.parent()
        while parent and not isinstance(parent, TerminalWidget):
            parent = parent.parent()
        
        if parent and isinstance(parent, TerminalWidget):
            # 現在のタブのインデックスを取得
            current_widget = parent.tab_widget.currentWidget()
            if current_widget == self:
                parent.stop_log_recording()
    
    def wheelEvent(self, event):
        """Ctrl+ホイールを設定経路へ回す

        QTextEdit の組込みズームは config を通らず、6〜32pt の制限も
        受けず、設定を再適用すると失われる。表示メニューの拡大/縮小と
        同じ意味になるよう、要求として上へ投げる。
        """
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                self.font_size_change_requested.emit(1 if delta > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    def _cursor_key(self, letter: str) -> str:
        """カーソルキーの送り方。

        アプリが ESC[?1h (DECCKM) を立てている間は SS3 形式
        (ESC O A) で送る。nano は矢印キーをこの形で待っている。
        """
        screen = getattr(self, "_screen", None)
        if screen is not None and screen.application_cursor_keys:
            return "\x1bO" + letter
        return "\x1b[" + letter

    def keyPressEvent(self, event: QKeyEvent):
        """キーイベントを処理"""
        if not self._input_enabled:
            return
        
        # 特殊キーの処理
        key = event.key()
        
        # 再接続待機中はEnter以外のキーを無視（切断中の送信でバナーが増殖するのを防ぐ）
        if self._reconnect_mode and key not in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return
        
        if key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter:
            # 再接続モードの場合は再接続シグナルを発行。
            # ここで待ちを解かない。解くのは接続に成功した時点
            # （MainWindow._on_connection_success）。先に解くと、再接続に
            # 失敗したときに待ちが戻らず、画面に残る「Enterキーを押すと
            # 再接続します」の案内どおりに操作できなくなる
            if self._reconnect_mode:
                self.reconnect_requested.emit()
                return
            # 通常モードの場合はSSHに送信
            self._queue_send('\r')
        elif key == Qt.Key.Key_Backspace:
            self._queue_send('\x7f')  # DEL文字
        elif key == Qt.Key.Key_Tab:
            self._queue_send('\t')
        elif key == Qt.Key.Key_Escape:
            self._queue_send('\x1b')
        elif key == Qt.Key.Key_Up:
            self._queue_send(self._cursor_key('A'))
        elif key == Qt.Key.Key_Down:
            self._queue_send(self._cursor_key('B'))
        elif key == Qt.Key.Key_Right:
            self._queue_send(self._cursor_key('C'))
        elif key == Qt.Key.Key_Left:
            self._queue_send(self._cursor_key('D'))
        else:
            # 通常の文字入力
            text = event.text()
            if text:
                self._queue_send(text)


class TerminalWidget(QWidget):
    # settings.terminal のキーと既定値。src/resources/default_config.json と
    # config_manager.py のフォールバック辞書に合わせてある。
    DEFAULT_TERMINAL_SETTINGS = {
        "background_color": "#000000",
        "text_color": "#FFFFFF",
        "font_family": "Consolas",
        "font_size": 10,
    }

    # フォントサイズの上下限（settings.terminal.font_size）。表示メニューの
    # 拡大/縮小（MainWindow）と設定ダイアログの QSpinBox（Task 7）が同じ範囲を
    # 使うため、数値の実体はここへ一本化する。
    FONT_SIZE_MIN = 6
    FONT_SIZE_MAX = 32

    # 表示文書に残す最大ブロック（行）数。Screen.history（5000 行）より
    # 多めに取り、画面領域（最大 200 行）を削らない余裕を持たせる
    MAX_DOCUMENT_BLOCKS = 20000

    # 1 ブロック (段落) に許す最大文字数。折り返しで続く履歴行は改行で
    # 切らずに繋ぐため、改行を一度も含まない出力ではブロックが 1 個の
    # まま伸び、MAX_DOCUMENT_BLOCKS が永久に効かない。実際の 1 行より
    # 十分大きく、かつ伸び続けさせない所で強制的に切る
    MAX_BLOCK_CHARS = 8192

    # タブが閉じられたときのシグナル（機器名を送信）
    tab_closed = pyqtSignal(str)
    # 表示中のタブが変わったことを知らせる（機器名。タブが無ければ空文字）。
    # SFTP パネルなど、機器に紐づく表示を追従させるために要る。
    current_tab_changed = pyqtSignal(str)
    # マクロ実行要求シグナル（機器名、マクロ名）
    macro_execute_requested = pyqtSignal(str, str)
    # Ctrl+ホイールでのフォントサイズ変更要求（回した向き: +1 / -1）
    font_size_change_requested = pyqtSignal(int)
    # マクロ設定画面要求シグナル（機器名）
    macro_settings_requested = pyqtSignal(str)
    # 実行中のマクロ停止要求シグナル（機器名）
    macro_stop_requested = pyqtSignal(str)
    # キープアライブ開始要求シグナル（機器名）
    keepalive_start_requested = pyqtSignal(str)
    # キープアライブ停止要求シグナル（機器名）
    keepalive_stop_requested = pyqtSignal(str)
    # 端末の行数・桁数が変わった（機器名, 桁, 行）。機器への通知に使う
    terminal_resized = pyqtSignal(str, int, int)

    def __init__(self):
        super().__init__()
        self._terminals: Dict[str, InteractiveTerminal] = {}  # 機器名 -> ターミナル
        self._log_files: Dict[str, object] = {}  # 機器名 -> ログファイルハンドル
        self._log_dialogs: Dict[str, object] = {}  # 機器名 -> ログ記録ダイアログ
        # ターミナルの外観設定。_create_terminal が参照するので _create_ui より先に持つ
        self._terminal_settings = dict(self.DEFAULT_TERMINAL_SETTINGS)
        # ウィンドウをドラッグ中のリサイズ連打を 1 回にまとめる
        from PyQt6.QtCore import QTimer
        self._pending_resizes = set()
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(200)
        self._resize_timer.timeout.connect(self._apply_pending_resizes)
        self._create_ui()
    
    def _create_ui(self):
        """UI作成"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # タブウィジェット
        self.tab_widget = QTabWidget()
        self.tab_widget.setTabsClosable(True)
        # 横ドラッグで並べ替えられる。機器の対応付けはタブ名と
        # ウィジェットで持っていて、位置には依存しない
        self.tab_widget.setMovable(True)
        self.tab_widget.tabCloseRequested.connect(self._close_tab)
        self.tab_widget.currentChanged.connect(self._on_current_tab_changed)
        
        # デフォルトのウェルカムタブを作成
        welcome_terminal = self._create_terminal()
        welcome_terminal.setPlainText(
            "ターミナル表示エリア\n"
            "接続先を選択して「接続」ボタンをクリックしてください。\n"
            "\n"
            "準備完了..."
        )
        self.tab_widget.addTab(welcome_terminal, "ホーム")
        
        layout.addWidget(self.tab_widget)
    
    def _create_terminal(self, interactive: bool = False) -> QTextEdit:
        """
        新しいターミナルウィジェットを作成
        
        Args:
            interactive: Trueの場合、キー入力可能なターミナルを作成
        
        Returns:
            QTextEdit: 設定済みのターミナルウィジェット
        """
        if interactive:
            terminal = InteractiveTerminal()
        else:
            terminal = QTextEdit()
            terminal.setReadOnly(True)

        # Undo 履歴は持たない。機器の出力を巻き戻す用途は無く（Ctrl+Z は
        # 機器へ送る）、画面内の上書き更新のたびに undo が積まれて
        # メモリが単調増加していた（実測: 20 万回の上書きで +70MB）
        terminal.setUndoRedoEnabled(False)
        # 文書の行数にも上限を置く。Screen.history の上限は文書へ写した
        # 後の行には効かず、受信行数のまま増え続けていた。超えた分は
        # Qt が先頭ブロックから捨てる。画面領域は末尾なので影響しない
        terminal.document().setMaximumBlockCount(self.MAX_DOCUMENT_BLOCKS)
        # 一度でも上限に達した（＝古い行が捨てられた）ことを覚えておく。
        # 「全ログ保存」の欠落警告をいまの blockCount だけで決めると、
        # 窓を 1 行縦に縮めるだけで上限を下回り、欠けたログが完全なもの
        # として黙って保存される
        terminal._log_truncated = False

        # フォント設定（_terminal_settings を参照する。設定変更後に作られる
        # タブも同じ外観になるようにするため）
        settings = self._terminal_settings
        font = QFont(settings["font_family"], settings["font_size"])
        font.setStyleHint(QFont.StyleHint.Monospace)
        terminal.setFont(font)

        # 配色設定
        palette = terminal.palette()
        palette.setColor(QPalette.ColorRole.Base, QColor(settings["background_color"]))
        palette.setColor(QPalette.ColorRole.Text, QColor(settings["text_color"]))
        terminal.setPalette(palette)

        return terminal

    @classmethod
    def normalize_terminal_settings(cls, settings) -> dict:
        """
        設定値を検証し、妥当でないものを既定値で埋める

        設定ダイアログもこの結果を表示・保存する。生値をそのまま扱うと、
        ターミナルの見た目と設定画面の表示が食い違い、何も変えずに OK を
        押しただけで壊れた値が書き戻される。

        config.json は手で編集できるため、空文字・0・不正な色名といった値が
        入ってくる。そのまま使うと読めない配色や不自然なフォントになるので、
        キーごとに型と値を確かめ、通らなかったものは既定値へ戻す。

        Args:
            settings: settings.terminal 相当の dict（None や dict 以外も受け付ける）

        Returns:
            4キーすべてが妥当な値で埋まった dict
        """
        merged = dict(cls.DEFAULT_TERMINAL_SETTINGS)
        if not isinstance(settings, dict):
            return merged

        family = settings.get("font_family")
        if isinstance(family, str) and family.strip():
            merged["font_family"] = family.strip()

        # bool は int の派生なので明示的に除く（True が 1pt になるのを防ぐ）。
        # 手編集で極端な値（例: 1000）が入っても描画が壊れないよう上下限も見る。
        size = settings.get("font_size")
        if (isinstance(size, int) and not isinstance(size, bool)
                and cls.FONT_SIZE_MIN <= size <= cls.FONT_SIZE_MAX):
            merged["font_size"] = size

        for key in ("background_color", "text_color"):
            value = settings.get(key)
            if isinstance(value, str) and QColor(value).isValid():
                merged[key] = value

        return merged

    def apply_terminal_settings(self, settings: dict):
        """
        ターミナルの外観設定を全タブへ適用する

        既存タブだけでなく、以降に作られるタブにも同じ設定が乗る。

        Args:
            settings: settings.terminal 相当の dict。欠けているキーは既定値を使う
        """
        merged = self.normalize_terminal_settings(settings)
        self._terminal_settings = merged

        font = QFont(merged["font_family"], merged["font_size"])
        font.setStyleHint(QFont.StyleHint.Monospace)
        background = QColor(merged["background_color"])
        text_color = QColor(merged["text_color"])

        # _terminals にはホームタブが入らないため、タブを直接走査する
        for i in range(self.tab_widget.count()):
            terminal = self.tab_widget.widget(i)
            terminal.setFont(font)
            palette = terminal.palette()
            palette.setColor(QPalette.ColorRole.Base, background)
            palette.setColor(QPalette.ColorRole.Text, text_color)
            terminal.setPalette(palette)

        # 文字の大きさが変われば収まる行数・桁数も変わる
        for device_name in self._terminals:
            self._schedule_grid_update(device_name)

    def current_terminal_settings(self) -> dict:
        """
        いま実際に適用されている外観設定を返す

        config の生値ではなくこちらを基準にすること。生値は手編集で壊れている
        可能性があり（文字列・null・bool・範囲外）、そのまま計算に使うと落ちる。

        Returns:
            正規化済みの設定のコピー
        """
        return dict(self._terminal_settings)

    def get_current_terminal(self) -> Optional[QTextEdit]:
        """現在表示中のタブのターミナルを返す（タブが無ければ None）"""
        return self.tab_widget.currentWidget()

    def create_terminal_tab(self, device_name: str) -> InteractiveTerminal:
        """
        新しいターミナルタブを作成
        
        Args:
            device_name: 機器名
        
        Returns:
            InteractiveTerminal: 作成されたターミナル
        """
        # 既にタブが存在する場合は、そのタブをアクティブにする
        if device_name in self._terminals:
            for i in range(self.tab_widget.count()):
                if self.tab_widget.tabText(i) == device_name:
                    self.tab_widget.setCurrentIndex(i)
                    # 再接続は新しいセッション。前の画面はそのまま記録と
                    # して文書に残し、端末状態 (パーサ・画面) は作り直す
                    self._attach_screen(self._terminals[device_name])
                    # 再接続待ちはここでは解かない。解くのは接続できた
                    # 時点（MainWindow._on_connection_success）。ここで
                    # 解くと、再接続に失敗したときに待ちが戻らず、画面の
                    # 「Enterキーを押すと再接続します」が効かなくなる
                    return self._terminals[device_name]
        
        # 接続機器がなく、ホームタブが残っている場合は、ホームタブを再利用
        if len(self._terminals) == 0 and self.tab_widget.count() == 1:
            home_tab = self.tab_widget.widget(0)
            if isinstance(home_tab, QTextEdit) and not isinstance(home_tab, InteractiveTerminal):
                # ホームタブを削除して新しいインタラクティブターミナルを作成。
                # ページごと捨てないと、接続と切断を繰り返すたびにホーム
                # タブ用の QTextEdit が非表示のまま 1 個ずつ積み上がる
                self._discard_page(0)
        
        # 新しいインタラクティブターミナルを作成
        terminal = self._create_terminal(interactive=True)
        self._terminals[device_name] = terminal
        
        # マクロ実行要求シグナルを接続
        terminal.font_size_change_requested.connect(
            self.font_size_change_requested.emit)
        terminal.macro_execute_requested.connect(
            lambda macro_name: self.macro_execute_requested.emit(device_name, macro_name)
        )
        
        # マクロ設定画面要求シグナルを接続
        terminal.macro_settings_requested.connect(
            lambda: self.macro_settings_requested.emit(device_name)
        )
        terminal.macro_stop_requested.connect(
            lambda: self.macro_stop_requested.emit(device_name)
        )
        
        # キープアライブ開始/停止要求シグナルを接続
        terminal.keepalive_start_requested.connect(
            lambda: self.keepalive_start_requested.emit(device_name)
        )
        terminal.keepalive_stop_requested.connect(
            lambda: self.keepalive_stop_requested.emit(device_name)
        )

        # ウィンドウの大きさに画面の格子を追従させる
        terminal.resized.connect(
            lambda: self._schedule_grid_update(device_name))
        
        # タブとして追加
        index = self.tab_widget.addTab(terminal, device_name)
        self.tab_widget.setCurrentIndex(index)

        self._attach_screen(terminal)
        return terminal

    # 端末の格子の大きさ。pty 要求 (vt100, 80x24) と合わせてある
    SCREEN_ROWS = 24
    SCREEN_COLS = 80

    def _attach_screen(self, terminal: QTextEdit) -> None:
        """端末状態 (パーサと画面) をこのタブへ付け直す。

        文書のいまの末尾から後ろを「今の画面」の領域とする。それより
        前は確定した記録で、二度と書き換えない。
        """
        rows, cols = self._grid_size(terminal)
        terminal._parser = vt.Parser()
        terminal._screen = Screen(rows, cols)
        region = QTextCursor(terminal.document())
        region.movePosition(QTextCursor.MoveOperation.End)
        terminal._region = region

    def _grid_size(self, terminal: QTextEdit):
        """いまの表示領域に収まる (行数, 桁数)。測れないときは 24x80。"""
        from PyQt6.QtGui import QFontMetrics
        metrics = QFontMetrics(terminal.font())
        char_w = metrics.horizontalAdvance("0")
        line_h = metrics.lineSpacing()
        width = terminal.viewport().width()
        height = terminal.viewport().height()
        if char_w <= 0 or line_h <= 0 or width < 2 * char_w or height < line_h:
            return self.SCREEN_ROWS, self.SCREEN_COLS
        return (max(5, min(200, height // line_h)),
                max(20, min(500, width // char_w)))

    def grid_size_for(self, device_name: str):
        """接続時の pty 要求 (RFC 4254 6.2) に使う (桁, 行)。"""
        terminal = self._terminals.get(device_name)
        if terminal is None:
            return self.SCREEN_COLS, self.SCREEN_ROWS
        rows, cols = self._grid_size(terminal)
        return cols, rows

    def _schedule_grid_update(self, device_name: str) -> None:
        self._pending_resizes.add(device_name)
        self._resize_timer.start()

    def _apply_pending_resizes(self) -> None:
        pending, self._pending_resizes = self._pending_resizes, set()
        for device_name in sorted(pending):
            self._apply_grid_size(device_name)

    def _apply_grid_size(self, device_name: str) -> None:
        """画面の格子を表示領域に合わせ、変わったら機器へ知らせる。"""
        terminal = self._terminals.get(device_name)
        if terminal is None:
            return
        screen = terminal._screen
        rows, cols = self._grid_size(terminal)
        if (rows, cols) == (screen.rows, screen.cols):
            return
        screen.set_size(rows, cols)
        self._render_screen(terminal)
        self.terminal_resized.emit(device_name, cols, rows)

    @staticmethod
    def _visible_cells(line):
        """行のセル列から、既定属性の末尾空白だけを落とす。

        色や反転の付いた空白 (nano のタイトルバーの帯) は表示に
        意味があるので残す。
        """
        n = len(line)
        while n and line[n - 1] == BLANK:
            n -= 1
        return line[:n]

    @staticmethod
    def _runs(cells):
        """同じ属性が続く区間ごとに (文字列, 属性) を返す。"""
        runs = []
        for ch, attr in cells:
            if runs and runs[-1][1] == attr:
                runs[-1][0].append(ch)
            else:
                runs.append(([ch], attr))
        return [("".join(chars), attr) for chars, attr in runs]

    def _colour(self, value, default):
        """セルの色番号を実際の色にする。"""
        if value is None:
            return default
        if isinstance(value, tuple):
            return QColor(*value)
        if value < 16:
            return QColor(ANSI_COLOURS[value])
        if value < 232:                     # 6x6x6 の色立方体
            value -= 16
            parts = (value // 36, value // 6 % 6, value % 6)
            return QColor(*(0 if p == 0 else 55 + p * 40 for p in parts))
        gray = 8 + (value - 232) * 10       # グレースケール
        return QColor(gray, gray, gray)

    def _char_format(self, attr) -> QTextCharFormat:
        fmt = QTextCharFormat()
        if attr == DEFAULT:
            return fmt                      # 空の書式 = パレットに従う
        settings = self._terminal_settings
        fg = self._colour(attr.fg, QColor(settings["text_color"]))
        bg = self._colour(attr.bg, QColor(settings["background_color"]))
        if attr.reverse:
            fg, bg = bg, fg
        fmt.setForeground(fg)
        if attr.bg is not None or attr.reverse:
            fmt.setBackground(bg)
        if attr.bold:
            fmt.setFontWeight(QFont.Weight.Bold)
        if attr.underline:
            fmt.setFontUnderline(True)
        return fmt

    def _paint_row(self, terminal: QTextEdit, offset: int, cells) -> None:
        """1 行ぶんのセル属性を、文書のその範囲へ塗る。

        書式だけを変えるので文字位置は動かず、範囲選択も壊れない。
        """
        painter = QTextCursor(terminal.document())
        for text, attr in self._runs(cells):
            painter.setPosition(offset)
            painter.setPosition(offset + _u16(text),
                                QTextCursor.MoveMode.KeepAnchor)
            painter.setCharFormat(self._char_format(attr))
            offset += _u16(text)

    def _render_screen(self, terminal: QTextEdit) -> None:
        """画面の中身を文書へ写す。

        文書 = [確定した記録 (履歴)] + [今の画面]。上から押し出された
        行を記録側へ差し込み、画面領域は変わった範囲だけ描き直す。
        """
        screen = terminal._screen
        region = terminal._region

        # 組み直しの直後は、文書に残っている「今の画面」が組み直し前の
        # ものなので、内容が一致しても同じ行とは限らない。境目を進める
        # 近道は使えない (使うと押し出された行が書かれずに消える)
        reflowed = screen.take_reflowed()

        for line, wrapped in screen.take_new_history():
            # 折り返しで続いている行は、改行で切らずに次の行と繋げる。
            # 切ると、窓を縮めている間に流れた出力が刻まれたまま記録に
            # 残り、窓を戻しても直らない
            cells = list(line) if wrapped else self._visible_cells(line)
            text = "".join(cell[0] for cell in cells)
            # 押し出された行は、画面領域の先頭として文書にもう書いて
            # ある。同じ内容なら書き直さず、記録との境目を進めるだけに
            # する。書き直すと画面領域が丸ごと入れ替わり、そこにある
            # 範囲選択が消える (機器がログを 1 行吐くだけで起きる)
            probe = QTextCursor(terminal.document())
            probe.setPosition(region.position())
            probe.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                               QTextCursor.MoveMode.KeepAnchor)
            if (not reflowed and not wrapped
                    and probe.selectedText() == text
                    and not probe.atEnd()):
                region.setPosition(probe.position() + 1)
                continue
            for run, attr in self._runs(cells):
                region.insertText(run, self._char_format(attr))
            if not wrapped:
                region.insertText("\n", QTextCharFormat())
            elif region.positionInBlock() >= self.MAX_BLOCK_CHARS:
                # 改行を一度も含まない出力 (バイナリの cat など) は、
                # 折り返し行を繋ぎ続けるかぎりブロックが 1 個のまま伸び、
                # MAX_DOCUMENT_BLOCKS が永久に効かない。文書もメモリも
                # 際限なく膨らみ、1 ブロックの組版が重くなって GUI が
                # 止まる。表示上の折り返し位置は変わるが、ここで切る
                region.insertText("\n", QTextCharFormat())

        cell_rows = [self._visible_cells(line) for line in screen.lines]
        # カーソルの行は、カーソルの桁まで空白を残す。プロンプト末尾の
        # 空白 ("Router# ") を落とすとキャレットが $ に張り付いて見える
        pad = screen.cursor_col - len(cell_rows[screen.cursor_row])
        if pad > 0:
            cell_rows[screen.cursor_row] = (
                cell_rows[screen.cursor_row] + [BLANK] * pad)
        rows = ["".join(cell[0] for cell in cells) for cells in cell_rows]
        # 画面は必ず行数ぶんの高さで描く。末尾の空行を詰めると、clear の
        # あとに履歴が下端へせり上がり「消えていない」ように見える。
        # 全画面アプリも、画面の一部しか窓に入らなくなる
        new_text = "\n".join(rows)
        start = region.position()

        # 変わった範囲だけ置き換える。全部消して入れ直すと、機器が
        # ログを吐くたびに画面内の範囲選択が消えてしまう
        probe = QTextCursor(terminal.document())
        probe.setPosition(start)
        probe.movePosition(QTextCursor.MoveOperation.End,
                           QTextCursor.MoveMode.KeepAnchor)
        old_text = probe.selectedText().replace("\u2029", "\n")
        touched = (0, -1)
        if old_text != new_text:
            prefix = 0
            limit = min(len(old_text), len(new_text))
            while prefix < limit and old_text[prefix] == new_text[prefix]:
                prefix += 1
            suffix = 0
            while (suffix < limit - prefix
                   and old_text[-1 - suffix] == new_text[-1 - suffix]):
                suffix += 1
            # 添字は Python の文字数なので、文書の位置へ直してから渡す
            probe.setPosition(start + _u16(old_text[:prefix]))
            probe.setPosition(start + _u16(old_text[:len(old_text) - suffix]),
                              QTextCursor.MoveMode.KeepAnchor)
            # 書式は空で入れる。insertText は挿入位置の書式を引き継ぐので、
            # 指定しないと直前の色や反転が新しい文字へ伝染する
            removed = _u16(old_text[prefix:len(old_text) - suffix])
            added = _u16(new_text[prefix:len(new_text) - suffix])
            before = terminal.document().characterCount()
            probe.insertText(new_text[prefix:len(new_text) - suffix],
                             QTextCharFormat())
            # 文書が上限 (MAX_DOCUMENT_BLOCKS) に達していると、この挿入で
            # Qt が文書の先頭ブロックを捨てる。QTextCursor である region や
            # probe は自動で詰まるが、int で控えた start は古い位置を指した
            # まま残る。そのままだと次の差し替え範囲・塗り直し位置・
            # キャレット位置がずれ、縦にリサイズするたびにスクロール
            # バックへ重複行と欠落が積み上がる。捨てられた文字数を
            # 数えて詰め直す
            dropped = (before - removed + added
                       - terminal.document().characterCount())
            if dropped:
                start -= dropped
            # 下の行との突き合わせは文書の位置で行うので、単位を揃える
            touched = (_u16(new_text[:prefix]),
                       _u16(new_text[:len(new_text) - suffix]))
        region.setPosition(start)

        # 変わった行に色・太字・反転を塗り直す
        dirty = screen.take_dirty()
        dirty.add(screen.cursor_row)    # カーソル桁の空白の伸縮ぶん
        # 書き換わった範囲の行も塗り直す。画面側が「変わっていない」と
        # 思っていても、入れ直した文字は書式を失っている
        at = 0
        for r, line in enumerate(rows):
            if at <= touched[1] and at + _u16(line) >= touched[0]:
                dirty.add(r)
            at += _u16(line) + 1
        offset = start
        for r in range(len(rows)):
            if r in dirty:
                self._paint_row(terminal, offset, cell_rows[r])
            offset += _u16(rows[r]) + 1

        # キャレット (点滅カーソル) を画面カーソルの位置へ。範囲選択の
        # 最中に動かすと選択が消えるので、そのときは触らない
        if not terminal.textCursor().hasSelection():
            pos = start
            for r in range(screen.cursor_row):
                pos += _u16(rows[r]) + 1
            # 桁ではなくセルで数える。全角は 2 セルで文書上は 1 文字、
            # 結合文字は 0 セルで文書上は 1 文字ぶん増えるので、文字列を
            # 桁で切るとキャレットがずれる
            row = cell_rows[screen.cursor_row][:screen.cursor_col]
            pos += _u16("".join(cell[0] for cell in row))
            caret = QTextCursor(terminal.document())
            caret.setPosition(pos)
            terminal.setTextCursor(caret)
        # 端末と同じく、常に下端へ寄せる。画面は文書の末尾 rows 行
        # なので、ここを見せることが「いま端末に映っているもの」を
        # 見せることになる。カーソルへ寄せると、全画面アプリでは
        # 画面の上半分しか窓に入らない
        bar = terminal.verticalScrollBar()
        bar.setValue(bar.maximum())

        # 上限に達していたら「切り詰めた」を立てたままにする。この後で
        # 窓を縮めて blockCount が下回っても、捨てた行は戻らない
        if terminal.document().blockCount() >= self.MAX_DOCUMENT_BLOCKS:
            terminal._log_truncated = True

    def show_notice(self, device_name: str, text: str) -> None:
        """アプリ自身の案内 (切断バナー・エラー文) を画面へ出す。

        端末では LF は「1 行下へ」であって行頭へは戻らない (戻すのは
        CR)。アプリの文言は普通の改行で書かれているので、ここで
        CRLF へ直す。直さないと案内が階段状にずれて出る。
        """
        self.append_output(device_name,
                           text.replace("\r\n", "\n")
                               .replace("\n", "\r\n"))

    def append_output(self, device_name: str, text: str) -> None:
        """
        指定した機器のターミナルにテキストを追加

        Args:
            device_name: 機器名
            text: 追加するテキスト
        """
        if device_name not in self._terminals:
            return
        terminal = self._terminals[device_name]

        events = terminal._parser.feed(text)
        terminal._screen.apply(events)
        self._render_screen(terminal)

        # 機器からの問い合わせ (カーソル位置・装置識別) に答える。
        # key_pressed はキー入力と同じ「機器へ送る文字」の経路
        for response in terminal._screen.take_responses():
            terminal._queue_send(response)

        if device_name in self._log_files:
            # タブは桁を作る文字なので落とすと表が潰れる
            logged = "".join(
                e.text if isinstance(e, vt.Print) else e.char
                for e in events
                if isinstance(e, vt.Print)
                or (isinstance(e, vt.Ctrl) and e.char in "\n\t"))
            if logged:
                try:
                    self._log_files[device_name].write(logged)
                    self._log_files[device_name].flush()
                except Exception as e:
                    self._abort_log_recording(device_name, e)

    def _abort_log_recording(self, device_name: str, error: Exception) -> None:
        """書き込みに失敗した記録を止めて、知らせる。

        stderr へ print するだけだと、ディスク満杯や共有フォルダの切断のあとも
        「記録中」の表示とフラグが残り、利用者は記録できていると思って作業を
        続ける。console=False の exe では stderr の出力先も無い。
        ハンドルを先に外すので、警告は失敗のたびではなく一度だけ出る。
        """
        from PyQt6.QtWidgets import QMessageBox

        from core import log_recording
        log_recording.stop(device_name)
        handle = self._log_files.pop(device_name, None)
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass   # 壊れたハンドルは閉じるのも失敗しうる
        terminal = self._terminals.get(device_name)
        if isinstance(terminal, InteractiveTerminal):
            terminal._is_recording = False
        dialog = self._log_dialogs.pop(device_name, None)
        if dialog is not None:
            dialog.close()
        QMessageBox.warning(
            self, "ログ記録",
            "%s のログ記録を停止しました。書き込みに失敗しました:\n%s\n\n"
            "記録は失敗する前の行までです。保存先の空きや接続を確認してから、"
            "記録を始め直してください。" % (device_name, error))
    
    def has_terminal(self, device_name: str) -> bool:
        """その機器名のターミナルタブが開いているかを返す

        Args:
            device_name: 機器名
        """
        return device_name in self._terminals

    def enable_reconnect(self, device_name: str, reconnect_callback):
        """
        再接続モードを有効にする
        
        Args:
            device_name: 機器名
            reconnect_callback: 再接続コールバック関数
        """
        if device_name in self._terminals:
            terminal = self._terminals[device_name]
            terminal.set_reconnect_mode(True)
            # 再接続要求シグナルをコールバックに接続（蓄積を防ぐため既存接続を切断）
            try:
                terminal.reconnect_requested.disconnect()
            except TypeError:
                pass
            terminal.reconnect_requested.connect(lambda: reconnect_callback(device_name))
    

    def _discard_page(self, index: int) -> None:
        """タブを外し、そのページを親から外して捨てる

        QTabWidget.removeTab はページを内部の QStackedWidget から外さない。
        残されたページは文書（受信した全出力・ウェルカム文）ごと非表示の
        まま生き続け、タブを閉じるたび・ホームタブを置き換えるたびに
        積み上がる。親から外して deleteLater で捨てる。

        Args:
            index: 外すタブのインデックス
        """
        widget = self.tab_widget.widget(index)
        self.tab_widget.removeTab(index)
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()

    def _close_tab(self, index: int) -> None:
        """
        タブを閉じる
        
        Args:
            index: タブのインデックス
        """
        # タブ名を取得
        tab_name = self.tab_widget.tabText(index)
        
        # ホームタブは閉じない（念のため）
        if tab_name == "ホーム":
            return
        
        # タブが閉じられることをMainWindowに通知（SSH切断のため）
        if tab_name in self._terminals:
            self.tab_closed.emit(tab_name)
        
        # 記録中なら止めてから閉じる。放っておくとファイルハンドルが
        # 開いたまま残り（Windows ではファイルがロックされたままになる）、
        # 宙に浮いたダイアログの停止ボタンが以後は別の機器を止めてしまう
        self._stop_log_recording_for(tab_name, notify=False)

        # 辞書から削除
        if tab_name in self._terminals:
            del self._terminals[tab_name]

        # タブを削除（ページごと捨てる）
        self._discard_page(index)

        # すべての接続が閉じられた場合、ホームタブを再作成
        if self.tab_widget.count() == 0:
            welcome_terminal = self._create_terminal()
            welcome_terminal.setPlainText(
                "ターミナル表示エリア\n"
                "接続先を選択して「接続」ボタンをクリックしてください。\n"
                "\n"
                "準備完了..."
            )
            self.tab_widget.addTab(welcome_terminal, "ホーム")
    
    @staticmethod
    def _default_log_dir() -> str:
        """保存先ダイアログの初期ディレクトリ（cwd/logs。作れなければ別の場所）

        読み取り専用の場所から起動した、logs という名前のファイルが既に
        ある、といった理由で cwd/logs を作れないことがある。ここで例外に
        すると保存先ダイアログが一度も出ず、書ける場所を選ぶ手段が無い。
        作れないときはホームへ落とす（保存先自体はダイアログで選べる）。
        """
        import os
        logs_dir = os.path.join(os.getcwd(), "logs")
        try:
            os.makedirs(logs_dir, exist_ok=True)
        except OSError:
            return os.path.expanduser("~")
        return logs_dir

    def _recording_device_using(self, file_path: str):
        """file_path を記録先にしている機器名を返す（無ければ None）

        記録中のファイルを別の記録や全ログ保存の保存先に選ぶと、open('w')
        で記録済みの内容が消え、以降は両者の書き込みが混在する。保存先を
        決めた直後にここで見て拒否する。

        判定そのものは core.log_recording に置いてある。端末以外の画面
        （SNMP のエクスポートなど）も同じ判定を使う必要があるため。
        """
        from core import log_recording
        return log_recording.device_using(file_path)

    def _warn_log_file_in_use(self, title: str, file_path: str, device_name: str):
        """記録中のファイルが選ばれたことを知らせる"""
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.warning(
            self,
            title,
            f"このファイルは {device_name} のログ記録に使用中です:\n{file_path}\n"
            "別のファイルを選ぶか、先にそのログ記録を停止してください。"
        )

    def save_current_log(self):
        """現在アクティブなターミナルのログを保存"""
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        from datetime import datetime
        import os
        
        # 現在のタブを取得
        current_index = self.tab_widget.currentIndex()
        if current_index < 0:
            return
        
        current_widget = self.tab_widget.widget(current_index)
        tab_name = self.tab_widget.tabText(current_index)
        
        # ホームタブの場合は保存しない
        if tab_name == "ホーム":
            QMessageBox.information(
                self,
                "ログ保存",
                "ホームタブにはログがありません。"
            )
            return
        
        # ターミナルのテキストを取得
        if isinstance(current_widget, (QTextEdit, InteractiveTerminal)):
            log_text = current_widget.toPlainText()
            
            if not log_text.strip():
                QMessageBox.information(
                    self,
                    "ログ保存",
                    "保存するログがありません。"
                )
                return
            
            # 表示文書は MAX_DOCUMENT_BLOCKS 行で頭から切り詰められる。
            # 保存するのはその toPlainText() なので、切り詰められた分は
            # 「全ログ保存」でも出てこない。黙って落とさず先に断る
            # いまの行数だけで決めない。上限に達して古い行を捨てた後でも、
            # 窓を縦に 1 行縮めれば blockCount は上限を下回る。捨てた事実の
            # ほうを見る
            blocks = current_widget.document().blockCount()
            if (blocks >= self.MAX_DOCUMENT_BLOCKS
                    or getattr(current_widget, "_log_truncated", False)):
                answer = QMessageBox.warning(
                    self,
                    "ログ保存",
                    f"画面に残っているのは直近 {blocks} 行だけです。\n"
                    "それより古い出力は表示から消えているため、保存されません。\n\n"
                    "全量が必要なときは「ログ記録」を使ってください。"
                    "受信のたびにファイルへ書き出すので、表示の上限に影響されません。\n\n"
                    "残っている分だけを保存しますか？",
                    QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Ok
                )
                if answer != QMessageBox.StandardButton.Ok:
                    return

            # デフォルトのファイル名を生成（機器名_日時.log）
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            default_filename = f"{tab_name}_{timestamp}.log"

            logs_dir = self._default_log_dir()

            # ファイル保存ダイアログを表示
            file_path, _ = QFileDialog.getSaveFileName(
                self,
                "ログファイル保存",
                os.path.join(logs_dir, default_filename),
                "ログファイル (*.log);;テキストファイル (*.txt);;すべてのファイル (*.*)"
            )
            
            if file_path:
                in_use_by = self._recording_device_using(file_path)
                if in_use_by is not None:
                    self._warn_log_file_in_use("ログ保存", file_path, in_use_by)
                    return

                # プログレスダイアログを表示してログを保存
                from .dialogs.log_save_dialog import LogSaveProgressDialog
                
                dialog = LogSaveProgressDialog(log_text, file_path, self)
                if dialog.exec():
                    QMessageBox.information(
                        self,
                        "ログ保存完了",
                        f"ログファイルを保存しました:\n{file_path}"
                    )
    
    def start_log_recording(self):
        """現在アクティブなターミナルのログ記録を開始"""
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        from datetime import datetime
        import os
        
        # 現在のタブを取得
        current_index = self.tab_widget.currentIndex()
        if current_index < 0:
            return
        
        current_widget = self.tab_widget.widget(current_index)
        tab_name = self.tab_widget.tabText(current_index)
        
        # ホームタブの場合は記録しない
        if tab_name == "ホーム":
            QMessageBox.information(
                self,
                "ログ記録",
                "ホームタブではログ記録できません。"
            )
            return
        
        # 既に記録中の場合は何もしない
        if tab_name in self._log_files:
            QMessageBox.information(
                self,
                "ログ記録",
                "既にログ記録中です。"
            )
            return
        
        # デフォルトのファイル名を生成（機器名_日時.log）
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_filename = f"{tab_name}_{timestamp}.log"

        logs_dir = self._default_log_dir()

        # ファイル保存ダイアログを表示
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "ログ記録ファイル選択",
            os.path.join(logs_dir, default_filename),
            "ログファイル (*.log);;テキストファイル (*.txt);;すべてのファイル (*.*)"
        )
        
        if file_path:
            in_use_by = self._recording_device_using(file_path)
            if in_use_by is not None:
                self._warn_log_file_in_use("ログ記録", file_path, in_use_by)
                return

            try:
                # ファイルを開く
                log_file = open(file_path, 'w', encoding='utf-8', buffering=1)  # 行バッファリング
                self._log_files[tab_name] = log_file
                from core import log_recording
                log_recording.start(tab_name, file_path)
                
                # ターミナルの記録フラグを設定
                if isinstance(current_widget, InteractiveTerminal):
                    current_widget._is_recording = True
                
                # ログ記録ダイアログを表示
                from .dialogs.log_recording_dialog import LogRecordingDialog
                dialog = LogRecordingDialog(tab_name, file_path, self)
                dialog.stop_requested.connect(self.stop_log_recording)
                dialog.show()
                self._log_dialogs[tab_name] = dialog
                
                QMessageBox.information(
                    self,
                    "ログ記録開始",
                    f"ログ記録を開始しました:\n{file_path}"
                )
                
            except Exception as e:
                QMessageBox.warning(
                    self,
                    "エラー",
                    f"ログファイルを開けませんでした:\n{str(e)}"
                )
    
    def stop_log_recording(self, device_name: str = None):
        """ログ記録を停止する

        Args:
            device_name: 止める機器。省略時は表示中のタブ（メニューや
                右クリックからの操作）。記録中ダイアログの停止ボタンは
                必ず自分の機器名を渡す。表示中のタブから引き直すと、
                2台を同時に記録しているときに別の機器を止めてしまう。
        """
        if device_name is None:
            current_index = self.tab_widget.currentIndex()
            if current_index < 0:
                return
            device_name = self.tab_widget.tabText(current_index)
        self._stop_log_recording_for(device_name)

    def _stop_log_recording_for(self, tab_name: str, notify: bool = True):
        """指定した機器のログ記録を止めて後始末する

        Args:
            tab_name: 止める機器
            notify: 完了の通知を出すか。タブを閉じたときの後始末では
                出さない（閉じる操作のたびにダイアログが出てしまう）
        """
        from PyQt6.QtWidgets import QMessageBox

        current_widget = self._terminals.get(tab_name)

        # ログファイルを閉じる
        if tab_name in self._log_files:
            # 後始末は close() より先に済ませる。close() は残った書き込みを
            # 吐き出すので、ディスク満杯・共有フォルダの切断で失敗しうる。
            # 後始末を close() の後ろに置くと、失敗したときだけ記録フラグと
            # ハンドルと「記録中」ダイアログが残り、記録を始め直そうとしても
            # 「既にログ記録中です。」で断られる（止める手段が無くなる）。
            handle = self._log_files.pop(tab_name)
            from core import log_recording
            log_recording.stop(tab_name)

            # ターミナルの記録フラグをクリア
            if isinstance(current_widget, InteractiveTerminal):
                current_widget._is_recording = False

            # ダイアログを閉じる。停止ボタン経由だと相手は自分でも
            # close() を呼んでいるので、二度閉じても平気にしておく
            dialog = self._log_dialogs.pop(tab_name, None)
            if dialog is not None:
                dialog.close()

            try:
                handle.close()
            except Exception as e:
                QMessageBox.warning(
                    self,
                    "エラー",
                    f"ログファイルを閉じる際にエラーが発生しました:\n{str(e)}"
                )
                return

            if notify:
                QMessageBox.information(
                    self,
                    "ログ記録停止",
                    "ログ記録を停止しました。"
                )
    
    def _on_current_tab_changed(self, index: int) -> None:
        """表示中のタブが変わったことを知らせる"""
        self.current_tab_changed.emit(self.get_current_tab_name())

    def get_current_tab_name(self) -> str:
        """
        現在アクティブなタブの名前を取得
        
        Returns:
            str: タブ名（タブがない場合は空文字列）
        """
        current_index = self.tab_widget.currentIndex()
        if current_index >= 0:
            return self.tab_widget.tabText(current_index)
        return ""
    
    def set_macro_list(self, device_name: str, macros: list):
        """
        指定した機器のマクロリストを設定
        
        Args:
            device_name: 機器名
            macros: マクロリスト
        """
        if device_name in self._terminals:
            self._terminals[device_name].set_macro_list(macros)
    
    def set_keepalive_status(self, device_name: str, active: bool):
        """
        指定した機器のキープアライブ状態を設定
        
        Args:
            device_name: 機器名
            active: キープアライブ動作中かどうか
        """
        if device_name in self._terminals:
            self._terminals[device_name].set_keepalive_status(active)

    def set_command_list_status(self, device_name: str, active: bool):
        """
        指定した機器のマクロ（コマンドリスト）実行状態を設定

        Args:
            device_name: 機器名
            active: 実行中かどうか
        """
        if device_name in self._terminals:
            self._terminals[device_name].set_command_list_status(active)