"""TFTPサーバー制御パネル"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QLineEdit, QSpinBox, QTextEdit, QGroupBox,
    QFileDialog, QMessageBox, QCheckBox, QProgressBar, QTableWidget,
    QTableWidgetItem, QHeaderView
)
from PyQt6.QtGui import QFont
from core.tftp_server import TFTPServerManager
from datetime import datetime
from ui import theme

class TFTPServerPanel(QWidget):
    """TFTPサーバー制御パネル"""

    # アプリの終了処理に入ったか（MainWindow.closeEvent が立てる）。
    # 立っている間はモーダルを開かない。終了処理は記録を救うために配送待ちの
    # シグナルをその場で配るので、サーバのスレッドが出したエラーもそこで
    # 届く。答えるまで終了が止まるうえ、そのときサーバは停止済み。
    _closing = False

    # TFTP は認証が無く、存在しないファイルへの RRQ だけでもログと履歴が
    # 1行ずつ増える。上限が無いと遠隔から叩き続けるだけでメモリを食い潰せる。
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
        self.tftp_server = TFTPServerManager(self)
        self._active = {}       # (ip,filename,dir) -> {"t0":epoch, "total":int, "row":int}
        import time as _t; self._time = _t
        self._init_ui()
        self._connect_signals()
        self._restore_settings()

    def _init_ui(self):
        """UIを初期化"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        title_label = QLabel("TFTP サーバー")
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title_label.setFont(title_font)
        layout.addWidget(title_label)
        # 設定グループ
        settings_group = QGroupBox("サーバー設定")
        settings_layout = QGridLayout()
        settings_layout.addWidget(QLabel("ポート番号:"), 0, 0)
        self.port_spin = QSpinBox()
        self.port_spin.setMinimum(1)
        self.port_spin.setMaximum(65535)
        self.port_spin.setValue(69)
        self.port_spin.setMaximumWidth(100)
        settings_layout.addWidget(self.port_spin, 0, 1)
        settings_layout.addWidget(QLabel("ルートディレクトリ:"), 1, 0)
        root_layout = QHBoxLayout()
        self.root_dir_edit = QLineEdit()
        self.root_dir_edit.setText("./tftp_root")
        # 余った幅は入力欄だけが受け取り、参照ボタンは自分の幅を保つ。
        # 以前は setMaximumWidth(60) でボタンの頭を押さえていたので、
        # 自然な幅 80px に対していつも 60px へ潰れ、押しにくかった
        root_layout.addWidget(self.root_dir_edit, 1)
        self.browse_btn = QPushButton("参照")
        self.browse_btn.clicked.connect(self._on_browse_directory)
        root_layout.addWidget(self.browse_btn, 0)
        settings_layout.addLayout(root_layout, 1, 1)
        settings_layout.addWidget(QLabel("転送許可:"), 2, 0)
        allow_layout = QHBoxLayout()
        self.allow_upload_check = QCheckBox("アップロード許可")
        self.allow_upload_check.setChecked(True)
        allow_layout.addWidget(self.allow_upload_check)
        self.allow_download_check = QCheckBox("ダウンロード許可")
        self.allow_download_check.setChecked(True)
        allow_layout.addWidget(self.allow_download_check)
        settings_layout.addLayout(allow_layout, 2, 1)
        settings_group.setLayout(settings_layout)
        layout.addWidget(settings_group)
        # 起動中に無効化する入力群。参照ボタンも含める。欄だけ無効にしても
        # setText() は効くので、起動中に参照を押すと画面のルートだけが変わり、
        # 実際に公開しているルートと食い違う
        self._inputs = [self.port_spin, self.root_dir_edit, self.browse_btn,
                        self.allow_upload_check, self.allow_download_check]
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
        log_layout.addWidget(self.log_text)
        clear_log_btn = QPushButton("ログをクリア")
        clear_log_btn.clicked.connect(self._on_clear_log)
        clear_log_btn.setMaximumWidth(120)
        log_layout.addWidget(clear_log_btn)
        log_group.setLayout(log_layout)
        layout.addWidget(log_group)
        # 説明文
        info_label = QLabel("💡 Ciscoからconfigをコピー: copy running-config tftp://[WindowsのIP]/[ファイル名]")
        info_label.setStyleSheet(theme.note_style(self))
        info_label.setWordWrap(True)
        layout.addWidget(info_label)
        layout.addStretch()

    def _connect_signals(self):
        """シグナルを接続"""
        self.tftp_server.started.connect(self._on_server_started)
        self.tftp_server.stopped.connect(self._on_server_stopped)
        self.tftp_server.error_occurred.connect(self._on_error)
        self.tftp_server.transfer_started.connect(self._on_tx_started)
        self.tftp_server.transfer_progress.connect(self._on_tx_progress)
        self.tftp_server.transfer_complete.connect(self._on_tx_complete)
        self.tftp_server.client_activity.connect(self._on_activity_event)
        self.tftp_server.transfer_interrupted.connect(self._on_transfer_interrupted)
        self.tftp_server.protocol_event.connect(self._on_protocol_event)

    def _restore_settings(self):
        """保存済み設定を復元"""
        settings = self.config_manager.get_server_settings("tftp_server")
        root_directory = settings.get("root_directory")
        # 既定は専用フォルダ。TFTP は無認証で 0.0.0.0 に待ち受けるため、
        # デスクトップ等の個人フォルダを既定にしてはいけない。
        self.root_dir_edit.setText(root_directory if root_directory else "./tftp_root")
        port = settings.get("port")
        if port:
            self.port_spin.setValue(port)
        self.allow_upload_check.setChecked(bool(settings.get("allow_upload", True)))
        self.allow_download_check.setChecked(bool(settings.get("allow_download", True)))

    def _on_browse_directory(self):
        """ディレクトリ参照ダイアログを表示"""
        directory = QFileDialog.getExistingDirectory(self, "ルートディレクトリを選択", self.root_dir_edit.text())
        if directory:
            self.root_dir_edit.setText(directory)

    def _on_start_server(self):
        """サーバーを起動"""
        port = self.port_spin.value()
        root_dir = self.root_dir_edit.text().strip()
        allow_upload = self.allow_upload_check.isChecked()
        allow_download = self.allow_download_check.isChecked()
        if not root_dir:
            QMessageBox.warning(self, "入力エラー", "ルートディレクトリを指定してください。")
            return
        # 起動前に設定を保存してからサーバーを起動
        self.config_manager.set_server_settings("tftp_server", {"root_directory": root_dir, "port": port,
            "allow_upload": allow_upload, "allow_download": allow_download})
        success = self.tftp_server.start(port, root_dir, allow_upload, allow_download)
        if not success:
            return  # エラーはシグナルで通知される
        self.start_btn.setVisible(False)
        self.stop_btn.setVisible(True)
        for w in self._inputs:
            w.setEnabled(False)

    def _on_stop_server(self):
        """サーバーを停止"""
        self.tftp_server.stop()
        self.start_btn.setVisible(True)
        self.stop_btn.setVisible(False)
        for w in self._inputs:
            w.setEnabled(True)

    def _on_server_started(self):
        """サーバー起動時の処理"""
        self.status_label.setText(f"🔵 起動中 (ポート {self.port_spin.value()})")
        self.status_label.setStyleSheet("color: #2196F3; font-weight: bold; font-size: 14px;")
        self._add_log(f"サーバー起動: ポート {self.port_spin.value()}, ルート {self.root_dir_edit.text()}")

    def _on_server_stopped(self):
        """サーバー停止時の処理"""
        self.status_label.setText("🔴 停止中")
        self.status_label.setStyleSheet("color: #f44336; font-weight: bold; font-size: 14px;")
        self._add_log("サーバー停止")

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

    def _on_transfer_interrupted(self, ip: str, filename: str, direction: str):
        """未完了で終わった転送。エラーではないので行だけ確定させる。

        確定させないと「転送中」の表示が残り続ける。ダイアログは出さない。

        利用者が止めた場合と、機器が ERROR を送って打ち切った場合の
        どちらもここへ来る。見分ける手がかりは通知に無いので、文言は
        理由に踏み込まない（FTP パネルと同じ「転送中断」）。
        """
        st = self._active.pop((ip, filename, direction), None)
        if st is not None:
            self.history.setItem(st["row"], 5, QTableWidgetItem("中断"))
        self._add_log("[%s] 転送中断: %s" % (ip, filename))

    def _on_protocol_event(self, ip: str, filename: str, reason: str,
                           direction: str = ""):
        """転送ごとのプロトコル事象。ログに残し、該当行だけを確定させる。

        機器が勝手に投げてくる要求（auto-install の RRQ など）でも起きるので、
        モーダルは出さない。出すと、繋がっているだけでダイアログが溢れる。
        どの機器から来たのかが分かるよう、送信元を必ず添える。
        """
        if filename:
            self._add_log("[%s] %s: %s" % (ip, reason, filename))
        else:
            self._add_log("[%s] %s" % (ip, reason))

        # 巻き添えにしない。方向まで一致する行だけを確定させる。
        directions = (direction,) if direction else ("upload", "download")
        for d in directions:
            st = self._active.pop((ip, filename, d), None)
            if st is not None:
                self.history.setItem(st["row"], 5, QTableWidgetItem("エラー"))

    def _on_error(self, error_message: str):
        """サーバ自体の障害。利用者が対処しないと先へ進めないので止める。

        起動失敗・ポート使用中・ファイアウォールの類だけがここへ来る。
        転送ごとの事象は _on_protocol_event が扱う。
        """
        self._add_log(f"エラー: {error_message}")
        if self._closing:
            # 終了処理の途中。ログだけ残して戻る（_closing の説明を参照）
            return
        QMessageBox.critical(self, "TFTPサーバー エラー", error_message)

    def _on_fw_allow(self):
        """手動でファイアウォール受信許可を追加（管理者昇格）。3CDaemon方式で通らない環境の復旧用。"""
        self._add_log("ファイアウォール許可を実行します（管理者昇格）...")
        ok, msg = self.tftp_server.fix_firewall(self.port_spin.value())
        # 「反映待ち」等の理由を潰さず、そのまま見せる
        self._add_log("ファイアウォール許可: %s (%s)" % ("完了" if ok else "未反映/失敗", msg))

    def _add_log(self, message: str):
        """ログにメッセージを追加(自動スクロール付き)"""
        self.log_text.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

    def _on_clear_log(self):
        """ログをクリア"""
        self.log_text.clear()

    def closeEvent(self, event):
        """パネルが閉じられる時の処理(実行中ならサーバーを停止)"""
        if self.tftp_server.is_running:
            self.tftp_server.stop()
        event.accept()
