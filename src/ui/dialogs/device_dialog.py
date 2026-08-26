"""機器追加/編集ダイアログ"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QComboBox, QPushButton,
    QGroupBox, QListWidget, QFileDialog, QMessageBox
)
from PyQt6.QtCore import Qt
from typing import Dict, List, Optional

class DeviceDialog(QDialog):
    """機器追加/編集ダイアログ"""
    
    def __init__(self, parent=None, groups: List[str] = None, device_data: Dict = None):
        """
        初期化
        
        Args:
            parent: 親ウィジェット
            groups: グループ名リスト
            device_data: 編集時の機器データ（新規追加時はNone）
        """
        super().__init__(parent)
        self.groups = groups or []
        self.device_data = device_data or {}
        self.is_edit_mode = device_data is not None
        
        self.setWindowTitle("機器編集" if self.is_edit_mode else "機器追加")
        self.setModal(True)
        self.resize(500, 600)
        
        self._create_ui()
        self._load_data()
    
    def _create_ui(self):
        """UI作成"""
        layout = QVBoxLayout(self)
        
        # 基本情報グループ
        basic_group = QGroupBox("基本情報")
        basic_layout = QFormLayout(basic_group)
        
        self.name_edit = QLineEdit()
        basic_layout.addRow("機器名:", self.name_edit)
        
        self.group_combo = QComboBox()
        self.group_combo.addItems(self.groups)
        basic_layout.addRow("グループ:", self.group_combo)
        
        layout.addWidget(basic_group)
        
        # 接続情報グループ
        conn_group = QGroupBox("接続情報")
        conn_layout = QFormLayout(conn_group)
        
        self.protocol_combo = QComboBox()
        self.protocol_combo.addItems(["ssh", "telnet", "console"])
        self.protocol_combo.currentTextChanged.connect(self._on_protocol_changed)
        conn_layout.addRow("プロトコル:", self.protocol_combo)
        
        self.host_edit = QLineEdit()
        conn_layout.addRow("ホスト:", self.host_edit)
        
        self.port_edit = QLineEdit()
        self.port_edit.setText("22")
        conn_layout.addRow("ポート:", self.port_edit)
        
        self.username_edit = QLineEdit()
        conn_layout.addRow("ユーザー名:", self.username_edit)
        
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        conn_layout.addRow("パスワード:", self.password_edit)
        
        # SSH鍵ファイル
        ssh_key_layout = QHBoxLayout()
        self.ssh_key_edit = QLineEdit()
        self.ssh_key_btn = QPushButton("参照")
        self.ssh_key_btn.clicked.connect(self._browse_ssh_key)
        ssh_key_layout.addWidget(self.ssh_key_edit)
        ssh_key_layout.addWidget(self.ssh_key_btn)
        self.ssh_key_label = QLabel("秘密鍵:")
        conn_layout.addRow(self.ssh_key_label, ssh_key_layout)
        
        layout.addWidget(conn_group)
        
        # 個別マクログループ
        macro_group = QGroupBox("個別マクロ")
        macro_layout = QVBoxLayout(macro_group)
        
        self.macro_list = QListWidget()
        macro_layout.addWidget(self.macro_list)
        
        macro_btn_layout = QHBoxLayout()
        self.macro_add_btn = QPushButton("追加")
        self.macro_edit_btn = QPushButton("編集")
        self.macro_delete_btn = QPushButton("削除")
        
        # TODO: マクロ編集ダイアログ実装後に有効化
        self.macro_add_btn.setEnabled(False)
        self.macro_edit_btn.setEnabled(False)
        self.macro_delete_btn.setEnabled(False)
        
        macro_btn_layout.addWidget(self.macro_add_btn)
        macro_btn_layout.addWidget(self.macro_edit_btn)
        macro_btn_layout.addWidget(self.macro_delete_btn)
        macro_layout.addLayout(macro_btn_layout)
        
        layout.addWidget(macro_group)
        
        # OK/キャンセルボタン
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        self.ok_btn = QPushButton("OK")
        self.ok_btn.clicked.connect(self._on_ok)
        self.cancel_btn = QPushButton("キャンセル")
        self.cancel_btn.clicked.connect(self.reject)
        
        button_layout.addWidget(self.ok_btn)
        button_layout.addWidget(self.cancel_btn)
        
        layout.addLayout(button_layout)
    
    def _load_data(self):
        """データを読み込み（編集モード時）"""
        if not self.is_edit_mode:
            # 新規追加時はDefaultグループを選択
            default_index = self.group_combo.findText("Default")
            if default_index >= 0:
                self.group_combo.setCurrentIndex(default_index)
            return
        
        self.name_edit.setText(self.device_data.get("name", ""))
        self.host_edit.setText(self.device_data.get("host", ""))
        self.port_edit.setText(str(self.device_data.get("port", 22)))
        self.username_edit.setText(self.device_data.get("username", ""))
        self.password_edit.setText(self.device_data.get("password", ""))
        self.ssh_key_edit.setText(self.device_data.get("ssh_key", ""))
        
        # プロトコル設定
        protocol = self.device_data.get("protocol", "ssh")
        index = self.protocol_combo.findText(protocol)
        if index >= 0:
            self.protocol_combo.setCurrentIndex(index)
        
        # マクロ読み込み
        for macro in self.device_data.get("macros", []):
            self.macro_list.addItem(macro.get("name", ""))
    
    def _on_protocol_changed(self, protocol: str):
        """プロトコル変更時の処理"""
        # SSHの場合のみ秘密鍵フィールドを有効化
        is_ssh = protocol == "ssh"
        self.ssh_key_edit.setEnabled(is_ssh)
        self.ssh_key_btn.setEnabled(is_ssh)
        self.ssh_key_label.setEnabled(is_ssh)
        
        # デフォルトポート設定
        if protocol == "ssh":
            self.port_edit.setText("22")
        elif protocol == "telnet":
            self.port_edit.setText("23")
        elif protocol == "console":
            self.port_edit.setText("")
            self.host_edit.setPlaceholderText("例: COM1 または /dev/ttyUSB0")
    
    def _browse_ssh_key(self):
        """SSH秘密鍵ファイルを参照"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "SSH秘密鍵を選択",
            "",
            "すべてのファイル (*)"
        )
        if file_path:
            self.ssh_key_edit.setText(file_path)
    
    def _on_ok(self):
        """OKボタン押下時の処理"""
        # 入力チェック
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "入力エラー", "機器名を入力してください。")
            return
        
        if not self.host_edit.text().strip():
            QMessageBox.warning(self, "入力エラー", "ホストを入力してください。")
            return
        
        # SSH はユーザー名が無いと必ず認証に失敗する。空のまま保存できると、
        # その機器は二度と繋がらないうえ、失敗の理由も分からない。
        # telnet は利用者名を送らない機器が多く、console では使わない。
        if (self.protocol_combo.currentText() == "ssh"
                and not self.username_edit.text().strip()):
            QMessageBox.warning(self, "入力エラー",
                                "SSH ではユーザー名が必要です。")
            return

        # ポート番号チェック（console以外）
        if self.protocol_combo.currentText() != "console":
            try:
                port = int(self.port_edit.text())
                if port < 1 or port > 65535:
                    raise ValueError
            except ValueError:
                QMessageBox.warning(self, "入力エラー", "ポート番号は1-65535の範囲で入力してください。")
                return
        
        self.accept()
    
    def get_device_data(self) -> Dict:
        """入力された機器データを取得"""
        port_text = self.port_edit.text()
        port = int(port_text) if port_text else 0
        
        return {
            "name": self.name_edit.text().strip(),
            "host": self.host_edit.text().strip(),
            "port": port,
            "protocol": self.protocol_combo.currentText(),
            "username": self.username_edit.text().strip(),
            "password": self.password_edit.text(),
            "ssh_key": self.ssh_key_edit.text().strip(),
            "macros": []  # TODO: マクロ機能実装後に対応
        }
    
    def get_selected_group(self) -> str:
        """選択されたグループ名を取得"""
        return self.group_combo.currentText()
