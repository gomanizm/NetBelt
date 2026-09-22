"""SFTPサーバー制御パネル"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QLineEdit, QSpinBox, QTextEdit, QGroupBox,
    QFileDialog, QMessageBox
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from core.sftp_server import SFTPServerManager
from datetime import datetime
from ui import log_export, theme


class SFTPServerPanel(QWidget):
    """SFTPサーバー制御パネル"""

    # アプリの終了処理に入ったか（MainWindow.closeEvent が立てる）。
    # 立っている間はモーダルを開かない。終了処理は記録を救うために配送待ちの
    # シグナルをその場で配るので、サーバのスレッドが出したエラーもそこで
    # 届く。答えるまで終了が止まるうえ、そのときサーバは停止済み。
    _closing = False

    # 認証前の接続でもログは増える。上限が無いと遠隔から叩き続けるだけで
    # メモリを食い潰せるため、頭打ちにする。Syslog パネル（1000件）に合わせた。
    MAX_LOG_LINES = 1000

    def __init__(self, parent=None):
        """
        初期化
        
        Args:
            parent: 親ウィジェット
        """
        super().__init__(parent)
        
        self.sftp_server = SFTPServerManager(self)
        self.connected_clients = 0
        
        # UI初期化
        self._init_ui()
        
        # シグナル接続
        self._connect_signals()
    
    def _init_ui(self):
        """UIを初期化"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # タイトル
        title_label = QLabel("SFTP サーバー")
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title_label.setFont(title_font)
        layout.addWidget(title_label)
        
        # 設定グループ
        settings_group = QGroupBox("サーバー設定")
        settings_layout = QGridLayout()
        
        # ポート番号
        settings_layout.addWidget(QLabel("ポート番号:"), 0, 0)
        self.port_spin = QSpinBox()
        self.port_spin.setMinimum(1024)
        self.port_spin.setMaximum(65535)
        self.port_spin.setValue(2222)
        self.port_spin.setMaximumWidth(100)
        settings_layout.addWidget(self.port_spin, 0, 1)
        
        # ルートディレクトリ
        settings_layout.addWidget(QLabel("ルートディレクトリ:"), 1, 0)
        root_layout = QHBoxLayout()
        self.root_dir_edit = QLineEdit()
        self.root_dir_edit.setText("./sftp_root")
        # 余った幅は入力欄だけが受け取り、参照ボタンは自分の幅を保つ。
        # 以前は setMaximumWidth(60) でボタンの頭を押さえていたので、
        # 自然な幅 80px に対していつも 60px へ潰れ、押しにくかった
        root_layout.addWidget(self.root_dir_edit, 1)
        self.browse_btn = QPushButton("参照")
        self.browse_btn.clicked.connect(self._on_browse_directory)
        root_layout.addWidget(self.browse_btn, 0)
        settings_layout.addLayout(root_layout, 1, 1)
        
        # ユーザー名
        settings_layout.addWidget(QLabel("ユーザー名:"), 2, 0)
        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("ユーザー名を入力")
        self.username_edit.setMaximumWidth(150)
        settings_layout.addWidget(self.username_edit, 2, 1)
        
        # パスワード
        settings_layout.addWidget(QLabel("パスワード:"), 3, 0)
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("パスワードを入力")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setMaximumWidth(150)
        settings_layout.addWidget(self.password_edit, 3, 1)
        
        settings_group.setLayout(settings_layout)
        layout.addWidget(settings_group)
        
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
        self.fw_allow_btn.setToolTip("接続が通らない場合に押してください。Windowsファイアウォールの受信許可を追加します（管理者昇格/UACが1回出ます）。")
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
        
        self.clients_label = QLabel("接続クライアント: 0")
        status_layout.addWidget(self.clients_label)
        
        status_group.setLayout(status_layout)
        layout.addWidget(status_group)
        
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
        info_label = QLabel(
            "💡 Cisco Routerからファイルをコピーする場合:\n"
            "Router# copy running-config sftp://[ユーザー名]@[WindowsのIP]:[ポート]/[ファイル名]"
        )
        info_label.setStyleSheet(theme.note_style(self))
        info_label.setWordWrap(True)
        layout.addWidget(info_label)
        
        # スペーサー
        layout.addStretch()
    
    def _connect_signals(self):
        """シグナルを接続"""
        self.sftp_server.started.connect(self._on_server_started)
        self.sftp_server.stopped.connect(self._on_server_stopped)
        self.sftp_server.client_connected.connect(self._on_client_connected)
        self.sftp_server.client_disconnected.connect(self._on_client_disconnected)
        self.sftp_server.client_activity.connect(self._on_activity_event)
        self.sftp_server.error_occurred.connect(self._on_error)
    
    def _on_browse_directory(self):
        """ディレクトリ参照ダイアログを表示"""
        directory = QFileDialog.getExistingDirectory(
            self,
            "ルートディレクトリを選択",
            self.root_dir_edit.text()
        )
        
        if directory:
            self.root_dir_edit.setText(directory)
    
    def _on_start_server(self):
        """サーバーを起動"""
        # 設定を取得
        port = self.port_spin.value()
        root_dir = self.root_dir_edit.text().strip()
        username = self.username_edit.text().strip()
        # パスワードは入力そのままを使う。前後の空白も資格情報の一部で、
        # 削ると画面に見えている文字列ではログインできない。
        # 未入力かどうかの判定だけ strip() で行う。
        password = self.password_edit.text()
        
        # 入力検証
        if not root_dir:
            QMessageBox.warning(self, "入力エラー", "ルートディレクトリを指定してください。")
            return
        
        if not username:
            QMessageBox.warning(self, "入力エラー", "ユーザー名を入力してください。")
            return
        
        if not password.strip():
            QMessageBox.warning(self, "入力エラー", "パスワードを入力してください。")
            return
        
        # サーバーを起動
        success = self.sftp_server.start(port, root_dir, username, password)
        
        if not success:
            # エラーはシグナルで通知される
            return
        
        # UI更新
        self.start_btn.setVisible(False)
        self.stop_btn.setVisible(True)
        self.port_spin.setEnabled(False)
        self.root_dir_edit.setEnabled(False)
        # 参照も止める。欄だけ無効にしても setText() は効くので、起動中に
        # 参照を押すと画面のルートだけが変わり、実公開ルートと食い違う
        self.browse_btn.setEnabled(False)
        self.username_edit.setEnabled(False)
        self.password_edit.setEnabled(False)
    
    def _on_stop_server(self):
        """サーバーを停止"""
        self.sftp_server.stop()
        
        # UI更新
        self.start_btn.setVisible(True)
        self.stop_btn.setVisible(False)
        self.port_spin.setEnabled(True)
        self.root_dir_edit.setEnabled(True)
        self.browse_btn.setEnabled(True)
        self.username_edit.setEnabled(True)
        self.password_edit.setEnabled(True)
    
    def _on_server_started(self):
        """サーバー起動時の処理"""
        port = self.port_spin.value()
        root_dir = self.root_dir_edit.text()
        
        self.status_label.setText(f"🔵 起動中 (ポート {port})")
        self.status_label.setStyleSheet("color: #2196F3; font-weight: bold; font-size: 14px;")
        
        self._add_log(f"サーバー起動: ポート {port}, ルート {root_dir}")
    
    def _on_server_stopped(self):
        """サーバー停止時の処理"""
        self.status_label.setText("🔴 停止中")
        self.status_label.setStyleSheet("color: #f44336; font-weight: bold; font-size: 14px;")
        
        self.connected_clients = 0
        self.clients_label.setText("接続クライアント: 0")
        
        self._add_log("サーバー停止")
    
    def _on_client_connected(self, client_ip: str):
        """クライアント接続時の処理"""
        self.connected_clients += 1
        self.clients_label.setText(f"接続クライアント: {self.connected_clients}")
        
        self._add_log(f"クライアント接続: {client_ip}")
    
    def _on_client_disconnected(self, client_ip: str):
        """クライアント切断時の処理"""
        self.connected_clients = max(0, self.connected_clients - 1)
        self.clients_label.setText(f"接続クライアント: {self.connected_clients}")
        
        self._add_log(f"クライアント切断: {client_ip}")
    
    def _on_activity_event(self, ip: str, message: str):
        """サーバーからのお知らせ（省略した通知の件数など）をログへ出す"""
        self._add_log("[%s] %s" % (ip, message) if ip else message)

    def _on_error(self, error_message: str):
        """エラー発生時の処理"""
        self._add_log(f"エラー: {error_message}")
        if self._closing:
            # 終了処理の途中。ログだけ残して戻る（_closing の説明を参照）
            return
        QMessageBox.critical(self, "SFTPサーバー エラー", error_message)
    
    def _on_fw_allow(self):
        """手動でファイアウォール受信許可を追加（管理者昇格）。

        起動時には触らない（TFTP/FTP と同じ 3CDaemon 方式）ので、
        Windows の初回プロンプトを拒否した等で通らない環境はここで直す。
        """
        self._add_log("ファイアウォール許可を実行します（管理者昇格）...")
        ok, msg = self.sftp_server.fix_firewall(self.port_spin.value())
        # 「反映待ち」等の理由を潰さず、そのまま見せる
        self._add_log("ファイアウォール許可: %s (%s)" % ("完了" if ok else "未反映/失敗", msg))

    def _add_log(self, message: str):
        """ログにメッセージを追加"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}"
        self.log_text.append(log_message)
        
        # 自動スクロール
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
    
    def _on_clear_log(self):
        """ログをクリア"""
        self.log_text.clear()
    
    def _on_export_log(self):
        """画面に出ているアクティビティログをファイルへ保存する"""
        log_export.export_log_text(self, self.log_text.toPlainText(),
                                   "sftp_log")
    
    def closeEvent(self, event):
        """パネルが閉じられる時の処理"""
        # サーバーが実行中の場合は停止
        if self.sftp_server.is_running:
            self.sftp_server.stop()
        event.accept()