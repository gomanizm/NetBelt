from PyQt6.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QTabWidget, QMenu
from PyQt6.QtGui import QFont, QColor, QPalette, QKeyEvent, QAction
from PyQt6.QtCore import Qt, pyqtSignal
from typing import Dict, Optional


class InteractiveTerminal(QTextEdit):
    """キー入力を処理できるインタラクティブなターミナル"""
    
    key_pressed = pyqtSignal(str)  # キー入力シグナル
    reconnect_requested = pyqtSignal()  # 再接続要求シグナル
    macro_execute_requested = pyqtSignal(str)  # マクロ実行要求シグナル（マクロ名）
    macro_settings_requested = pyqtSignal()  # マクロ設定画面要求シグナル
    keepalive_start_requested = pyqtSignal()  # キープアライブ開始要求シグナル
    keepalive_stop_requested = pyqtSignal()  # キープアライブ停止要求シグナル
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(False)
        self._input_enabled = False
        self._reconnect_mode = False  # 再接続待機モード
        self._is_recording = False  # ログ記録中フラグ
        self._macro_list = []  # 利用可能なマクロリスト
        self._keepalive_active = False  # キープアライブ動作中フラグ
    
    def set_keepalive_status(self, active: bool):
        """キープアライブの状態を設定"""
        self._keepalive_active = active
    
    def set_input_enabled(self, enabled: bool):
        """入力の有効/無効を切り替え"""
        self._input_enabled = enabled
    
    def set_reconnect_mode(self, enabled: bool):
        """再接続モードを切り替え"""
        self._reconnect_mode = enabled
        if enabled:
            self._input_enabled = True  # 再接続モードではEnterキーを受け付ける
    
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
    
    def custom_paste(self):
        """カスタムペースト機能 - ペーストされたテキストをSSHセッションに送信"""
        from PyQt6.QtWidgets import QApplication
        
        clipboard = QApplication.clipboard()
        text = clipboard.text()
        
        if text and self._input_enabled:
            # ペーストされたテキストを1文字ずつ送信
            for char in text:
                if char == '\n' or char == '\r':
                    # 改行の場合は\rとして送信
                    self.key_pressed.emit('\r')
                else:
                    self.key_pressed.emit(char)
    
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
        paste_action.setEnabled(self._input_enabled)
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
            # 再接続モードの場合は再接続シグナルを発行
            if self._reconnect_mode:
                self._reconnect_mode = False
                self.reconnect_requested.emit()
                return
            # 通常モードの場合はSSHに送信
            self.key_pressed.emit('\r')
        elif key == Qt.Key.Key_Backspace:
            self.key_pressed.emit('\x7f')  # DEL文字
        elif key == Qt.Key.Key_Tab:
            self.key_pressed.emit('\t')
        elif key == Qt.Key.Key_Escape:
            self.key_pressed.emit('\x1b')
        elif key == Qt.Key.Key_Up:
            self.key_pressed.emit('\x1b[A')
        elif key == Qt.Key.Key_Down:
            self.key_pressed.emit('\x1b[B')
        elif key == Qt.Key.Key_Right:
            self.key_pressed.emit('\x1b[C')
        elif key == Qt.Key.Key_Left:
            self.key_pressed.emit('\x1b[D')
        else:
            # 通常の文字入力
            text = event.text()
            if text:
                self.key_pressed.emit(text)


class TerminalWidget(QWidget):
    # settings.terminal のキーと既定値。src/resources/default_config.json と
    # config_manager.py のフォールバック辞書に合わせてある。
    DEFAULT_TERMINAL_SETTINGS = {
        "background_color": "#000000",
        "text_color": "#FFFFFF",
        "font_family": "Consolas",
        "font_size": 10,
    }

    # タブが閉じられたときのシグナル（機器名を送信）
    tab_closed = pyqtSignal(str)
    # マクロ実行要求シグナル（機器名、マクロ名）
    macro_execute_requested = pyqtSignal(str, str)
    # マクロ設定画面要求シグナル（機器名）
    macro_settings_requested = pyqtSignal(str)
    # キープアライブ開始要求シグナル（機器名）
    keepalive_start_requested = pyqtSignal(str)
    # キープアライブ停止要求シグナル（機器名）
    keepalive_stop_requested = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        self._terminals: Dict[str, InteractiveTerminal] = {}  # 機器名 -> ターミナル
        self._log_files: Dict[str, object] = {}  # 機器名 -> ログファイルハンドル
        self._log_dialogs: Dict[str, object] = {}  # 機器名 -> ログ記録ダイアログ
        # ターミナルの外観設定。_create_terminal が参照するので _create_ui より先に持つ
        self._terminal_settings = dict(self.DEFAULT_TERMINAL_SETTINGS)
        self._create_ui()
    
    def _create_ui(self):
        """UI作成"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # タブウィジェット
        self.tab_widget = QTabWidget()
        self.tab_widget.setTabsClosable(True)
        self.tab_widget.tabCloseRequested.connect(self._close_tab)
        
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

    def apply_terminal_settings(self, settings: dict):
        """
        ターミナルの外観設定を全タブへ適用する

        既存タブだけでなく、以降に作られるタブにも同じ設定が乗る。

        Args:
            settings: settings.terminal 相当の dict。欠けているキーは既定値を使う
        """
        merged = dict(self.DEFAULT_TERMINAL_SETTINGS)
        for key in self.DEFAULT_TERMINAL_SETTINGS:
            if settings.get(key) is not None:
                merged[key] = settings[key]
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
                    return self._terminals[device_name]
        
        # 接続機器がなく、ホームタブが残っている場合は、ホームタブを再利用
        if len(self._terminals) == 0 and self.tab_widget.count() == 1:
            home_tab = self.tab_widget.widget(0)
            if isinstance(home_tab, QTextEdit) and not isinstance(home_tab, InteractiveTerminal):
                # ホームタブを削除して新しいインタラクティブターミナルを作成
                self.tab_widget.removeTab(0)
        
        # 新しいインタラクティブターミナルを作成
        terminal = self._create_terminal(interactive=True)
        self._terminals[device_name] = terminal
        
        # マクロ実行要求シグナルを接続
        terminal.macro_execute_requested.connect(
            lambda macro_name: self.macro_execute_requested.emit(device_name, macro_name)
        )
        
        # マクロ設定画面要求シグナルを接続
        terminal.macro_settings_requested.connect(
            lambda: self.macro_settings_requested.emit(device_name)
        )
        
        # キープアライブ開始/停止要求シグナルを接続
        terminal.keepalive_start_requested.connect(
            lambda: self.keepalive_start_requested.emit(device_name)
        )
        terminal.keepalive_stop_requested.connect(
            lambda: self.keepalive_stop_requested.emit(device_name)
        )
        
        # タブとして追加
        index = self.tab_widget.addTab(terminal, device_name)
        self.tab_widget.setCurrentIndex(index)
        
        return terminal
    
    def append_output(self, device_name: str, text: str) -> None:
        """
        指定した機器のターミナルにテキストを追加
        
        Args:
            device_name: 機器名
            text: 追加するテキスト
        """
        if device_name in self._terminals:
            terminal = self._terminals[device_name]
            from PyQt6.QtGui import QTextCursor
            
            # 制御コードを処理（カーソル位置も変更される可能性がある）
            processed_text = self._process_control_codes(terminal, text)
            
            # テキストがある場合のみ挿入
            # 描画は _process_control_codes 内で完了している（戻り値はログ用）
            if processed_text:
                # ログ記録中の場合はファイルに書き込み
                if device_name in self._log_files:
                    try:
                        self._log_files[device_name].write(processed_text)
                        self._log_files[device_name].flush()  # 即座にディスクに書き込む
                    except Exception as e:
                        import sys
                        print(f"[ERROR] ログ書き込みエラー: {str(e)}", file=sys.stderr)
    
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
    
    def _process_control_codes(self, terminal: QTextEdit, text: str) -> str:
        """機器から届いた制御コードを解釈して画面へ反映する。

        端末（Cisco IOS 等）は行編集を「上書き描画」で行う。
          - \b はカーソルを左へ動かすだけで、文字は消さない
          - \b + 空白 + \b が来たときだけ、その位置の文字が消える
          - 行の途中に文字を入れると、残り全体を送り直してから \b で戻ってくる
        そのため通常文字は、行末なら追記、途中なら既存文字を置換する。

        Args:
            terminal: 描画先のターミナル
            text: 受信テキスト

        Returns:
            str: ログ記録用に、画面へ出したテキスト
        """
        import re
        from PyQt6.QtGui import QTextCursor

        text = text.replace('\x00', '')   # NUL は捨てる（BEL は OSC 終端に使うため後段で処理）

        logged = []          # ログへ残す内容（画面に出した文字と改行）
        cursor = terminal.textCursor()

        def put(s):
            """カーソル位置へ書く。行末なら追記、途中なら上書き（端末と同じ挙動）。"""
            for ch in s:
                if not cursor.atBlockEnd():
                    cursor.deleteChar()      # 上書き（挿入すると行が伸びてしまう）
                cursor.insertText(ch)

        i = 0
        n = len(text)
        while i < n:
            ch = text[i]

            # --- エスケープシーケンス ---
            if ch == '\x1b':
                # CSI: ESC [ <パラメータ 0x30-0x3F> <中間 0x20-0x2F> <終端 0x40-0x7E>
                match = re.match(r'\x1b\[([0-9:;<=>?]*)([ -/]*)([@-~])', text[i:])
                if match:
                    params_str, _intermediate, command = match.groups()
                    # '?' 等で始まるプライベートパラメータ（DECSET/DECRST。bash の
                    # ブラケットペースト ESC[?2004h など）は解釈せず読み飛ばす
                    private = bool(params_str) and params_str[0] in '<=>?'
                    params = [] if private else (params_str.split(';') if params_str else [])
                    first = params[0] if params else ''
                    try:
                        # パラメータ省略時、移動系は 1、消去系は 0 が既定
                        num = int(first) if first else 1
                        mode = int(first) if first else 0
                    except ValueError:
                        # SGR の ':' 区切り副パラメータなど。移動・消去系では起きない
                        num, mode = 1, 0
                    if private:
                        pass                    # 解釈しない（読み飛ばすだけ）
                    elif command == 'D':        # カーソル左
                        for _ in range(num):
                            cursor.movePosition(QTextCursor.MoveOperation.Left)
                    elif command == 'C':        # カーソル右
                        for _ in range(num):
                            cursor.movePosition(QTextCursor.MoveOperation.Right)
                    elif command == 'K':        # 行内の消去
                        if mode == 1:           # 行頭からカーソルまで
                            cursor.movePosition(QTextCursor.MoveOperation.StartOfLine,
                                                QTextCursor.MoveMode.KeepAnchor)
                        elif mode == 2:         # 行全体
                            cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
                            cursor.movePosition(QTextCursor.MoveOperation.EndOfLine,
                                                QTextCursor.MoveMode.KeepAnchor)
                        else:                   # 0: カーソルから行末まで
                            cursor.movePosition(QTextCursor.MoveOperation.EndOfLine,
                                                QTextCursor.MoveMode.KeepAnchor)
                        cursor.removeSelectedText()
                    elif command == 'J':        # 画面の消去（NX-OS が行編集で使う）
                        if mode == 1:           # 先頭からカーソルまで
                            cursor.movePosition(QTextCursor.MoveOperation.Start,
                                                QTextCursor.MoveMode.KeepAnchor)
                        elif mode == 2:         # 画面全体
                            cursor.movePosition(QTextCursor.MoveOperation.Start)
                            cursor.movePosition(QTextCursor.MoveOperation.End,
                                                QTextCursor.MoveMode.KeepAnchor)
                        else:                   # 0: カーソルから末尾まで
                            cursor.movePosition(QTextCursor.MoveOperation.End,
                                                QTextCursor.MoveMode.KeepAnchor)
                        cursor.removeSelectedText()
                    i += len(match.group())
                    continue
                # OSC: ESC ] ... 終端は BEL または ST(ESC \)。ウィンドウタイトル等
                match = re.match(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', text[i:])
                if match:
                    i += len(match.group())
                    continue
                # 2バイト系: ESC ( B（文字集合指定）, ESC = / ESC >（キーパッドモード）等
                match = re.match(r'\x1b[ -/]*[0-~]', text[i:])
                if match:
                    i += len(match.group())
                    continue
                # 未知、または途中で切れたシーケンス。ESC 1文字だけ捨てて必ず前進する
                # （ここで前進しないと無限ループになり、アプリが固まる）
                i += 1
                continue

            # --- ベル ---
            if ch == '\x07':
                i += 1
                continue

            # --- バックスペース ---
            # 端末では \b は「カーソルを左へ動かす」だけ。文字を消すときは、機器が
            # 空白で上書きしてから \b で戻る列を送ってくるので、特別扱いは不要。
            if ch in ('\b', '\x08'):
                bs = 0
                j = i
                while j < n and text[j] in ('\b', '\x08'):
                    bs += 1
                    j += 1
                for _ in range(bs):
                    cursor.movePosition(QTextCursor.MoveOperation.Left)
                i = j
                continue

            # --- 改行 ---
            if ch == '\n':
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertText('\n')
                logged.append('\n')
                i += 1
                continue

            # --- 復帰 ---
            if ch == '\r':
                j = i
                while j < n and text[j] == '\r':
                    j += 1
                if j < n and text[j] == '\n':
                    cursor.movePosition(QTextCursor.MoveOperation.End)
                    cursor.insertText('\n')
                    logged.append('\n')
                    i = j + 1
                    continue
                cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
                i = j
                continue

            # --- 通常文字（連続分をまとめて書く）---
            j = i
            while j < n and text[j] not in ('\x1b', '\x07', '\b', '\x08', '\r', '\n'):
                j += 1
            chunk = text[i:j]
            put(chunk)
            logged.append(chunk)
            i = j

        terminal.setTextCursor(cursor)
        terminal.ensureCursorVisible()
        return ''.join(logged)


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
        
        # 辞書から削除
        if tab_name in self._terminals:
            del self._terminals[tab_name]
        
        # タブを削除
        self.tab_widget.removeTab(index)
        
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
            
            # デフォルトのファイル名を生成（機器名_日時.log）
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            default_filename = f"{tab_name}_{timestamp}.log"
            
            # logsフォルダのパスを取得（存在しない場合は作成）
            logs_dir = os.path.join(os.getcwd(), "logs")
            os.makedirs(logs_dir, exist_ok=True)
            
            # ファイル保存ダイアログを表示
            file_path, _ = QFileDialog.getSaveFileName(
                self,
                "ログファイル保存",
                os.path.join(logs_dir, default_filename),
                "ログファイル (*.log);;テキストファイル (*.txt);;すべてのファイル (*.*)"
            )
            
            if file_path:
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
        
        # logsフォルダのパスを取得（存在しない場合は作成）
        logs_dir = os.path.join(os.getcwd(), "logs")
        os.makedirs(logs_dir, exist_ok=True)
        
        # ファイル保存ダイアログを表示
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "ログ記録ファイル選択",
            os.path.join(logs_dir, default_filename),
            "ログファイル (*.log);;テキストファイル (*.txt);;すべてのファイル (*.*)"
        )
        
        if file_path:
            try:
                # ファイルを開く
                log_file = open(file_path, 'w', encoding='utf-8', buffering=1)  # 行バッファリング
                self._log_files[tab_name] = log_file
                
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
    
    def stop_log_recording(self):
        """現在アクティブなターミナルのログ記録を停止"""
        from PyQt6.QtWidgets import QMessageBox
        
        # 現在のタブを取得
        current_index = self.tab_widget.currentIndex()
        if current_index < 0:
            return
        
        current_widget = self.tab_widget.widget(current_index)
        tab_name = self.tab_widget.tabText(current_index)
        
        # ログファイルを閉じる
        if tab_name in self._log_files:
            try:
                self._log_files[tab_name].close()
                del self._log_files[tab_name]
                
                # ターミナルの記録フラグをクリア
                if isinstance(current_widget, InteractiveTerminal):
                    current_widget._is_recording = False
                
                # ダイアログを閉じる
                if tab_name in self._log_dialogs:
                    dialog = self._log_dialogs[tab_name]
                    dialog.close()
                    del self._log_dialogs[tab_name]
                
                QMessageBox.information(
                    self,
                    "ログ記録停止",
                    "ログ記録を停止しました。"
                )
                
            except Exception as e:
                QMessageBox.warning(
                    self,
                    "エラー",
                    f"ログファイルを閉じる際にエラーが発生しました:\n{str(e)}"
                )
    
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