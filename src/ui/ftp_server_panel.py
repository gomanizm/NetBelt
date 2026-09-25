"""FTPサーバー制御パネル"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QLineEdit, QSpinBox, QTextEdit, QGroupBox,
    QFileDialog, QMessageBox, QCheckBox, QProgressBar, QTableWidget,
    QTableWidgetItem, QHeaderView
)
from PyQt6.QtGui import QFont
from core.ftp_server import FTPServerManager
from ui import log_export, plain_log, theme
from datetime import datetime

class FTPServerPanel(QWidget):
    """FTPサーバー制御パネル"""

    # アプリの終了処理に入ったか（MainWindow.closeEvent が立てる）。
    # 立っている間はモーダルを開かない。終了処理は記録を救うために配送待ちの
    # シグナルをその場で配るので、サーバのスレッドが出したエラーもそこで
    # 届く。答えるまで終了が止まるうえ、そのときサーバは停止済み。
    _closing = False

    # ログも転送履歴も、認証を通らない相手の要求だけで増やせる。上限が
    # 無いと遠隔から叩き続けるだけでメモリを食い潰せるため、頭打ちにする。
    # 行数は Syslog パネル（1000件）に合わせた。
    MAX_LOG_LINES = 1000
    MAX_HISTORY_ROWS = 1000

    def __init__(self, parent=None, config_manager=None):
        """初期化。config_managerがNoneの場合は新規のConfigManagerを生成する。"""
        super().__init__(parent)
        if config_manager is None:
            from core.config_manager import ConfigManager
            self.config_manager = ConfigManager()
        else:
            self.config_manager = config_manager
        self.ftp_server = FTPServerManager(self)
        self._active = {}       # (ip,filename,dir) -> {"t0":epoch, "total":int, "row":int}
        import time as _t; self._time = _t
        self._init_ui()
        self._connect_signals()
        self._restore_settings()

    def _init_ui(self):
        """UIを初期化"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        title_label = QLabel("FTP サーバー")
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title_label.setFont(title_font)
        layout.addWidget(title_label)
        # 設定グループ
        settings_group = QGroupBox("サーバー設定")
        settings_layout = QGridLayout()
        settings_layout.addWidget(QLabel("ポート番号:"), 0, 0)
        self.port_spin = QSpinBox(); self.port_spin.setRange(1, 65535); self.port_spin.setValue(21); self.port_spin.setMaximumWidth(100)
        settings_layout.addWidget(self.port_spin, 0, 1)
        settings_layout.addWidget(QLabel("ルートディレクトリ:"), 1, 0)
        root_layout = QHBoxLayout()
        self.root_dir_edit = QLineEdit("./ftp_root")
        # 余った幅は入力欄だけが受け取り、参照ボタンは自分の幅を保つ。
        # 以前は setMaximumWidth(60) でボタンの頭を押さえていたので、
        # 自然な幅 80px に対していつも 60px へ潰れ、押しにくかった
        root_layout.addWidget(self.root_dir_edit, 1)
        self.browse_btn = QPushButton("参照")
        self.browse_btn.clicked.connect(self._on_browse_directory)
        root_layout.addWidget(self.browse_btn, 0)
        settings_layout.addLayout(root_layout, 1, 1)
        settings_layout.addWidget(QLabel("ユーザー名:"), 2, 0)
        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("ユーザー名を入力")
        self.username_edit.setMaximumWidth(150)
        settings_layout.addWidget(self.username_edit, 2, 1)
        settings_layout.addWidget(QLabel("パスワード:"), 3, 0)
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("パスワードを入力")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password); self.password_edit.setMaximumWidth(150)
        settings_layout.addWidget(self.password_edit, 3, 1)
        self.anonymous_check = QCheckBox("匿名を許可")
        settings_layout.addWidget(self.anonymous_check, 4, 1)
        # ログインの許可と書き込みの許可を分ける。束ねると、匿名を許した
        # つもりで誰でもファイルを置き換えられる状態になる。
        self.anonymous_write_check = QCheckBox("匿名からの書き込みを許可")
        self.anonymous_write_check.setToolTip(
            "機器から copy running-config ftp://… で送るときに必要です。\n"
            "許可すると、資格情報なしでルートディレクトリへファイルを置けます。")
        settings_layout.addWidget(self.anonymous_write_check, 5, 1)
        self.anonymous_check.toggled.connect(
            self.anonymous_write_check.setEnabled)
        self.anonymous_write_check.setEnabled(self.anonymous_check.isChecked())
        settings_layout.addWidget(QLabel("passiveポート範囲:"), 6, 0)
        passive_layout = QHBoxLayout()
        self.passive_lo_spin = QSpinBox(); self.passive_lo_spin.setRange(1024, 65535); self.passive_lo_spin.setValue(50100)
        self.passive_hi_spin = QSpinBox(); self.passive_hi_spin.setRange(1024, 65535); self.passive_hi_spin.setValue(50150)
        passive_layout.addWidget(self.passive_lo_spin)
        passive_layout.addWidget(QLabel("-"))
        passive_layout.addWidget(self.passive_hi_spin)
        passive_layout.addStretch()
        settings_layout.addLayout(passive_layout, 6, 1)
        settings_group.setLayout(settings_layout)
        layout.addWidget(settings_group)
        # 起動中に無効化する入力群。参照ボタンも含める（欄だけ無効にしても
        # setText() は効くので、画面のルートと実公開ルートが食い違う）
        self._inputs = [self.port_spin, self.root_dir_edit, self.browse_btn,
            self.username_edit, self.password_edit,
            self.anonymous_check, self.anonymous_write_check,
            self.passive_lo_spin, self.passive_hi_spin]
        # 制御ボタン
        button_layout = QHBoxLayout()
        self.start_btn = QPushButton("▶ サーバー起動")
        self.start_btn.clicked.connect(self._on_start_server)
        self.start_btn.setStyleSheet("QPushButton { background-color: #4CAF50; color: white; padding: 8px; font-weight: bold; }")
        button_layout.addWidget(self.start_btn)
        self.stop_btn = QPushButton("⬛ サーバー停止")
        self.stop_btn.clicked.connect(self._on_stop_server)
        self.stop_btn.setVisible(False)
        self.stop_btn.setStyleSheet("QPushButton { background-color: #f44336; color: white; padding: 8px; font-weight: bold; }")
        button_layout.addWidget(self.stop_btn)
        layout.addLayout(button_layout)
        # 手動FW許可（3CDaemon方式で通らない時の復旧用・押した時だけ管理者昇格/UAC）
        fw_layout = QHBoxLayout()
        self.fw_allow_btn = QPushButton("ファイアウォールで許可（管理者）")
        self.fw_allow_btn.setToolTip("転送が通らない場合に押してください。Windowsファイアウォールの受信許可を追加します（管理者昇格/UACが1回出ます）。")
        self.fw_allow_btn.clicked.connect(self._on_fw_allow)
        fw_layout.addWidget(self.fw_allow_btn)
        fw_layout.addStretch()
        layout.addLayout(fw_layout)
        # ステータス表示
        status_group = QGroupBox("サーバー状態")
        status_layout = QVBoxLayout()
        self.status_label = QLabel("🔴 停止中")
        self.status_label.setStyleSheet("color: #f44336; font-weight: bold; font-size: 14px;")
        status_layout.addWidget(self.status_label)
        status_group.setLayout(status_layout)
        layout.addWidget(status_group)
        # 転送履歴（進捗もここで表現。単一の進行中枠は廃止＝複数同時転送のチラつき解消）
        history_group = QGroupBox("転送履歴")
        history_layout = QVBoxLayout()
        self.history = QTableWidget(0, 6)
        self.history.setHorizontalHeaderLabels(["時刻", "方向", "ファイル名", "サイズ", "レート", "状況"])
        self.history.horizontalHeader().setStretchLastSection(True)
        self.history.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.history.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history.setMaximumHeight(300)
        history_layout.addWidget(self.history)
        history_group.setLayout(history_layout)
        layout.addWidget(history_group)
        # アクティビティログ
        log_group = QGroupBox("アクティビティログ")
        log_layout = QVBoxLayout()
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(150)
        # 行数の上限。超えた分は Qt が先頭ブロックから捨てる
        self.log_text.document().setMaximumBlockCount(self.MAX_LOG_LINES)
        self.log_text.setStyleSheet("font-family: Consolas, monospace; font-size: 9pt;")
        # ボタンはログのすぐ上に左寄せ1行（SNMP の Trap 受信と同じ形）。
        # 行末の addStretch() が無いと、余った幅がボタン自身に配られて
        # 横へ間延びする
        log_btn_layout = QHBoxLayout()
        self.export_log_btn = QPushButton("エクスポート")
        self.export_log_btn.clicked.connect(self._on_export_log)
        log_btn_layout.addWidget(self.export_log_btn)
        self.clear_log_btn = QPushButton("クリア")
        self.clear_log_btn.clicked.connect(self._on_clear_log)
        log_btn_layout.addWidget(self.clear_log_btn)
        log_btn_layout.addStretch()
        log_layout.addLayout(log_btn_layout)
        log_layout.addWidget(self.log_text)
        log_group.setLayout(log_layout)
        layout.addWidget(log_group)
        # 説明文
        info_label = QLabel("💡 Ciscoからconfigをコピー: copy running-config ftp://user:pass@[WindowsのIP]/[ファイル名]（機器側は ip ftp passive 推奨）")
        info_label.setStyleSheet(theme.note_style(self))
        info_label.setWordWrap(True)
        layout.addWidget(info_label)
        layout.addStretch()

    def _connect_signals(self):
        """シグナルを接続"""
        self.ftp_server.started.connect(self._on_server_started); self.ftp_server.stopped.connect(self._on_server_stopped)
        self.ftp_server.error_occurred.connect(self._on_error)
        self.ftp_server.client_activity.connect(self._on_activity_event)
        self.ftp_server.transfer_started.connect(self._on_tx_started)
        self.ftp_server.transfer_progress.connect(self._on_tx_progress)
        self.ftp_server.transfer_complete.connect(self._on_tx_complete)
        self.ftp_server.transfer_interrupted.connect(self._on_transfer_interrupted)

    def _restore_settings(self):
        """保存済み設定を復元"""
        settings = self.config_manager.get_server_settings("ftp_server")
        # 既定は専用フォルダ（個人フォルダを公開しないため）
        self.root_dir_edit.setText(settings.get("root_directory") or "./ftp_root")
        if settings.get("port"): self.port_spin.setValue(settings["port"])
        if settings.get("username"): self.username_edit.setText(settings["username"])
        if settings.get("password"): self.password_edit.setText(settings["password"])
        anonymous = bool(settings.get("anonymous", False))
        self.anonymous_check.setChecked(anonymous)
        # 書き込みの可否を持っていない古い設定は、これまでどおり
        # 書き込みを許した状態で読む。黙って読み取り専用にすると、
        # 機器からのアップロードがある日から通らなくなる。
        # 新規（匿名も未設定）は許可しない側から始める。
        self.anonymous_write_check.setChecked(
            bool(settings.get("anonymous_write", anonymous)))
        self.anonymous_write_check.setEnabled(anonymous)
        if settings.get("passive_low"): self.passive_lo_spin.setValue(settings["passive_low"])
        if settings.get("passive_high"): self.passive_hi_spin.setValue(settings["passive_high"])

    def _on_browse_directory(self):
        """ディレクトリ参照ダイアログを表示"""
        directory = QFileDialog.getExistingDirectory(self, "ルートディレクトリを選択", self.root_dir_edit.text())
        if directory:
            self.root_dir_edit.setText(directory)

    def _on_start_server(self):
        """サーバーを起動"""
        port = self.port_spin.value()
        root_dir = self.root_dir_edit.text().strip()
        username = self.username_edit.text().strip()
        # パスワードは入力そのままを使う。前後の空白も資格情報の一部で、
        # 削ると画面の表示ではログインできず、削った値が保存されるので
        # 次回以降も食い違う。未入力かどうかの判定だけ strip() で行う。
        password = self.password_edit.text()
        anonymous = self.anonymous_check.isChecked()
        anonymous_write = anonymous and self.anonymous_write_check.isChecked()
        lo = self.passive_lo_spin.value()
        hi = self.passive_hi_spin.value()
        if not root_dir:
            QMessageBox.warning(self, "入力エラー", "ルートディレクトリを指定してください。")
            return
        if not anonymous and (not username or not password.strip()):
            QMessageBox.warning(self, "入力エラー", "匿名を許可しない場合は、ユーザー名とパスワードを入力してください。")
            return
        # 起動前に設定を保存してからサーバーを起動
        self.config_manager.set_server_settings("ftp_server", {"root_directory": root_dir, "port": port,
            "username": username, "password": password, "anonymous": anonymous,
            "anonymous_write": anonymous_write, "passive_low": lo, "passive_high": hi})
        success = self.ftp_server.start(port, root_dir, username, password, anonymous=anonymous,
                                        passive_ports=(lo, hi), anonymous_write=anonymous_write)
        if not success:
            return  # エラーはシグナルで通知される
        self.start_btn.setVisible(False); self.stop_btn.setVisible(True)
        for w in self._inputs: w.setEnabled(False)

    def _on_stop_server(self):
        """サーバーを停止"""
        self.ftp_server.stop()
        self.start_btn.setVisible(True); self.stop_btn.setVisible(False)
        for w in self._inputs: w.setEnabled(True)

    def _on_server_started(self):
        """サーバー起動時の処理"""
        self.status_label.setText(f"🔵 起動中 (ポート {self.port_spin.value()})")
        self.status_label.setStyleSheet("color: #2196F3; font-weight: bold; font-size: 14px;")
        self._add_log(f"サーバー起動: ポート {self.port_spin.value()}, ルート {self.root_dir_edit.text()}")

    def _on_server_stopped(self):
        """サーバー停止時の処理"""
        self.status_label.setText("🔴 停止中")
        self.status_label.setStyleSheet("color: #f44336; font-weight: bold; font-size: 14px;")
        # 進行中のまま残った行を確定させる。放置すると "1%" 等の表示が
        # 止めたあとも残り続ける
        for st in self._active.values():
            self.history.setItem(st["row"], 5, QTableWidgetItem("中断"))
        self._active.clear()
        self._add_log("サーバー停止")

    def _on_transfer_interrupted(self, ip: str, filename: str, direction: str):
        """未完了で終わった転送。エラーではないので行だけ確定させる。

        確定させないと、途中の進捗表示のまま残り続ける。ダイアログは出さない。
        """
        st = self._active.pop((ip, filename, direction), None)
        if st is not None:
            self.history.setItem(st["row"], 5, QTableWidgetItem("中断"))
        self._add_log("[%s] 転送中断: %s" % (ip, filename))

    def _on_activity_event(self, ip: str, msg: str):
        """クライアントアクティビティ通知の処理"""
        self._add_log("[%s] %s" % (ip, msg))

    def _fmt_bytes(self, n):
        """バイト数を読みやすい単位文字列に変換"""
        for u in ("B", "KB", "MB", "GB"):
            if n < 1024 or u == "GB": return "%.1f%s" % (n, u)
            n /= 1024.0

    def _on_tx_started(self, ip, filename, total, direction):
        """転送開始: 履歴行を追加しプログレスバーを初期化"""
        key = (ip, filename, direction)
        row = self.history.rowCount(); self.history.insertRow(row)
        arrow = "↓" if direction == "download" else "↑"
        status = "0%" if total else "転送中"
        vals = [self._time.strftime("%H:%M:%S"), "%s %s" % (arrow, ip), filename,
                self._fmt_bytes(total) if total else "—", "—", status]
        for c, v in enumerate(vals): self.history.setItem(row, c, QTableWidgetItem(v))
        self._active[key] = {"t0": self._time.time(), "total": total, "row": row}
        self._trim_history()
        self._add_log("[%s] %s 転送開始: %s" % (ip, "取得" if direction == "download" else "受信", filename))

    def _on_tx_progress(self, ip, filename, done, total, direction):
        """転送中: プログレスバーとレート表示を更新"""
        st = self._active.get((ip, filename, direction))
        if not st: return
        row = st["row"]
        dt = max(1e-6, self._time.time() - st["t0"])
        rate = done / dt
        status = ("%d%%" % min(100, int(done * 100 / total))) if total else "転送中"
        self.history.setItem(row, 4, QTableWidgetItem("%s/s" % self._fmt_bytes(rate)))
        self.history.setItem(row, 5, QTableWidgetItem(status))

    def _on_tx_complete(self, ip, filename, done, total, direction):
        """転送完了: 履歴行を確定(無ければ新規作成)しプログレスバーを100%にする"""
        st = self._active.pop((ip, filename, direction), None)
        arrow = "↓" if direction == "download" else "↑"
        if st:
            dt = max(1e-6, self._time.time() - st["t0"]); rate = done / dt; row = st["row"]
            self.history.setItem(row, 3, QTableWidgetItem(self._fmt_bytes(done)))
            self.history.setItem(row, 4, QTableWidgetItem("%s/s" % self._fmt_bytes(rate)))
            self.history.setItem(row, 5, QTableWidgetItem("完了"))
        else:
            # 開始イベントが無い完了(FTPは完了のみ発火): 履歴行をその場で作る(レートは不明)
            row = self.history.rowCount(); self.history.insertRow(row)
            vals = [self._time.strftime("%H:%M:%S"), "%s %s" % (arrow, ip), filename,
                    self._fmt_bytes(done), "—", "完了"]
            for c, v in enumerate(vals):
                self.history.setItem(row, c, QTableWidgetItem(v))
            self._trim_history()
        self._add_log("[%s] 転送完了: %s (%s)" % (ip, filename, self._fmt_bytes(done)))

    def _trim_history(self):
        """転送履歴の行数を上限まで切り詰める（古い行から捨てる）。

        行を捨てると残りの行番号が前へずれるので、進行中の転送が覚えている
        行番号も同じだけ繰り上げる。ずらさないと、その転送の進捗が別の
        転送の行を上書きしてしまう。捨てられた行を指していた転送は追跡を
        やめる（完了時にその場で新しい行を作る）。
        """
        excess = self.history.rowCount() - self.MAX_HISTORY_ROWS
        if excess <= 0:
            return
        for _ in range(excess):
            self.history.removeRow(0)
        for key in [k for k, st in self._active.items() if st["row"] < excess]:
            del self._active[key]
        for st in self._active.values():
            st["row"] -= excess

    def _on_error(self, error_message: str):
        """エラー発生時の処理"""
        self._add_log(f"エラー: {error_message}")
        for st in self._active.values():
            self.history.setItem(st["row"], 5, QTableWidgetItem("エラー"))
        self._active.clear()
        if self._closing:
            # 終了処理の途中。ログだけ残して戻る（_closing の説明を参照）
            return
        QMessageBox.critical(self, "FTPサーバー エラー", error_message)

    def _on_fw_allow(self):
        """手動でファイアウォール受信許可を追加（管理者昇格）。3CDaemon方式で通らない環境の復旧用。"""
        self._add_log("ファイアウォール許可を実行します（管理者昇格）...")
        ok, msg = self.ftp_server.fix_firewall(
            self.port_spin.value(),
            (self.passive_lo_spin.value(), self.passive_hi_spin.value()))
        # 「反映待ち」等の理由を潰さず、そのまま見せる
        self._add_log("ファイアウォール許可: %s (%s)" % ("完了" if ok else "未反映/失敗", msg))

    def _add_log(self, message: str):
        """ログにメッセージを追加(自動スクロール付き)

        append() ではなく平文で積む。届いた名前に <br> があると 1 件の通知が
        2 行に割れ、偽の記録に見えた（plain_log の説明を参照）。
        """
        plain_log.append_line(
            self.log_text,
            f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def _on_clear_log(self):
        """ログをクリア"""
        self.log_text.clear()

    def _on_export_log(self):
        """画面に出ているアクティビティログをファイルへ保存する"""
        log_export.export_log_text(self, self.log_text.toPlainText(),
                                   "ftp_log")

    def closeEvent(self, event):
        """パネルが閉じられる時の処理(実行中ならサーバーを停止)"""
        if self.ftp_server.is_running:
            self.ftp_server.stop()
        event.accept()
