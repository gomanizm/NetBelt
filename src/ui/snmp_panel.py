"""
SNMPパネル（GET/WALK + Trap受信統合版）
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableView, QHeaderView,
    QPushButton, QLineEdit, QLabel, QComboBox, QGroupBox,
    QGridLayout, QMenu, QFileDialog, QMessageBox,
    QTabWidget, QSpinBox, QTreeView, QProgressDialog, QTextEdit
)
from PyQt6.QtCore import Qt, QAbstractTableModel, QModelIndex, QThread, pyqtSignal
from PyQt6.QtGui import QAction, QStandardItemModel, QStandardItem
from datetime import datetime
import json
from core.mib_resolver import get_resolver, MIBResolver


class SNMPResultTableModel(QAbstractTableModel):
    """SNMP GET/WALK結果テーブルモデル"""
    
    def __init__(self):
        super().__init__()
        self.results = []
        self.headers = ["OID", "Type", "Value"]
    
    def rowCount(self, parent=QModelIndex()):
        return len(self.results)
    
    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)
    
    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.results[index.row()][index.column()]
        return None
    
    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
        return None
    
    def set_results(self, results):
        self.beginResetModel()
        self.results = results
        self.endResetModel()
    
    def clear_results(self):
        self.beginResetModel()
        self.results.clear()
        self.endResetModel()
    
    def get_all_results(self):
        return self.results.copy()




class MIBLoaderThread(QThread):
    """MIB読み込みスレッド"""
    progress = pyqtSignal(str)  # ステータスメッセージ
    finished_signal = pyqtSignal(bool)  # 完了シグナル（成功/失敗）
    
    def run(self):
        """MIBファイルを読み込み"""
        try:
            self.progress.emit("MIBを読み込み中...")
            
            # MIBResolverを初期化（シングルトンが未初期化の場合のみ実行される）
            resolver = get_resolver()
            
            self.progress.emit("完了")
            self.finished_signal.emit(True)
        except Exception as e:
            self.progress.emit(f"エラー: {str(e)}")
            self.finished_signal.emit(False)


class SNMPPanel(QWidget):
    """SNMPパネル"""

    # v3 認証コンボの選択肢。(表示ラベル, core へ渡すキー) の組。
    # キーは core.snmp_manager の V3_*_PROTOCOL_NAMES と一致させること。
    AUTH_PROTOCOL_CHOICES = (
        ("なし", "none"),
        ("MD5", "MD5"),
        ("SHA-1", "SHA"),
        ("SHA-224", "SHA-224"),
        ("SHA-256", "SHA-256"),
        ("SHA-384", "SHA-384"),
        ("SHA-512", "SHA-512"),
    )
    PRIV_PROTOCOL_CHOICES = (
        ("なし", "none"),
        ("DES", "DES"),
        ("3DES", "3DES"),
        ("AES-128", "AES-128"),
        ("AES-192", "AES-192"),
        ("AES-256", "AES-256"),
    )

    def __init__(self, parent=None, config_manager=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self.snmp_manager = None
        self.mib_loaded = False  # MIB読み込み済みフラグ
        self.mib_loading = False  # MIB読み込み中フラグ
        
        self.result_model = SNMPResultTableModel()
        self.trap_tree_model = QStandardItemModel()
        self.trap_data_list = []  # 完全なTrapデータ（エクスポート用）
        
        self._init_ui()
        
        # アプリ起動時にバックグラウンドでMIBを読み込み開始
        self._start_background_mib_loading()
    
    def _init_ui(self):
        """UIの初期化"""
        layout = QVBoxLayout()
        layout.setContentsMargins(5, 5, 5, 5)
        
        # メインタブ
        main_tabs = QTabWidget()
        
        # GET/WALKタブ
        get_walk_widget = self._create_get_walk_tab()
        main_tabs.addTab(get_walk_widget, "GET / WALK")
        
        # Trap受信タブ
        trap_widget = self._create_trap_tab()
        main_tabs.addTab(trap_widget, "Trap受信")
        
        layout.addWidget(main_tabs)
        self.setLayout(layout)
    
    def _create_get_walk_tab(self):
        """GET/WALKタブの作成"""
        widget = QWidget()
        layout = QVBoxLayout()
        
        # 接続設定
        conn_group = QGroupBox("接続設定")
        conn_layout = QGridLayout()
        conn_layout.addWidget(QLabel("ホスト:"), 0, 0)
        self.host_edit = QLineEdit()
        conn_layout.addWidget(self.host_edit, 0, 1)
        conn_layout.addWidget(QLabel("ポート:"), 0, 2)
        self.port_spinbox = QSpinBox()
        self.port_spinbox.setRange(1, 65535)
        self.port_spinbox.setValue(161)
        conn_layout.addWidget(self.port_spinbox, 0, 3)
        conn_layout.addWidget(QLabel("バージョン:"), 1, 0)
        self.version_combo = QComboBox()
        self.version_combo.addItems(["v2c", "v1", "v3"])
        self.version_combo.currentTextChanged.connect(self._on_version_changed)
        conn_layout.addWidget(self.version_combo, 1, 1)
        conn_group.setLayout(conn_layout)
        layout.addWidget(conn_group)
        
        # 認証タブ
        self.auth_tabs = QTabWidget()
        
        # v1/v2c認証
        v2c_widget = QWidget()
        v2c_layout = QGridLayout()
        v2c_layout.addWidget(QLabel("Community:"), 0, 0)
        self.community_edit = QLineEdit("public")
        v2c_layout.addWidget(self.community_edit, 0, 1)
        v2c_layout.setRowStretch(1, 1)  # 空白を下に押しやる
        v2c_widget.setLayout(v2c_layout)
        self.auth_tabs.addTab(v2c_widget, "v1/v2c認証")

        # v3認証
        v3_widget = QWidget()
        v3_layout = QGridLayout()
        v3_layout.addWidget(QLabel("ユーザ名:"), 0, 0)
        self.v3_username_edit = QLineEdit()
        v3_layout.addWidget(self.v3_username_edit, 0, 1)

        v3_layout.addWidget(QLabel("認証方式:"), 1, 0)
        self.v3_auth_combo = QComboBox()
        for label, key in self.AUTH_PROTOCOL_CHOICES:
            self.v3_auth_combo.addItem(label, key)
        v3_layout.addWidget(self.v3_auth_combo, 1, 1)

        v3_layout.addWidget(QLabel("認証パスワード:"), 1, 2)
        self.v3_auth_password_edit = QLineEdit()
        self.v3_auth_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        v3_layout.addWidget(self.v3_auth_password_edit, 1, 3)

        v3_layout.addWidget(QLabel("暗号方式:"), 2, 0)
        self.v3_priv_combo = QComboBox()
        for label, key in self.PRIV_PROTOCOL_CHOICES:
            self.v3_priv_combo.addItem(label, key)
        v3_layout.addWidget(self.v3_priv_combo, 2, 1)

        v3_layout.addWidget(QLabel("暗号パスワード:"), 2, 2)
        self.v3_priv_password_edit = QLineEdit()
        self.v3_priv_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        v3_layout.addWidget(self.v3_priv_password_edit, 2, 3)

        v3_layout.setRowStretch(3, 1)
        v3_widget.setLayout(v3_layout)
        self.auth_tabs.addTab(v3_widget, "v3認証")

        # プリセット
        preset_widget = QWidget()
        preset_layout = QVBoxLayout()
        preset_layout.setContentsMargins(5, 5, 5, 5)
        preset_h = QHBoxLayout()
        preset_h.addWidget(QLabel("プリセット:"))
        self.preset_combo = QComboBox()
        self.preset_combo.addItems([
            "カスタム",
            "sysDescr (1.3.6.1.2.1.1.1.0)",
            "sysName (1.3.6.1.2.1.1.5.0)",
            "sysUpTime (1.3.6.1.2.1.1.3.0)"
        ])
        self.preset_combo.currentTextChanged.connect(self._on_preset_changed)
        preset_h.addWidget(self.preset_combo)
        preset_h.addStretch()
        preset_layout.addLayout(preset_h)
        preset_layout.addStretch()  # 空白を下に押しやる
        preset_widget.setLayout(preset_layout)
        self.auth_tabs.addTab(preset_widget, "プリセットOID")
        
        layout.addWidget(self.auth_tabs)

        # 起動直後の選択（v2c）に合わせておく。呼ばないと両方のタブが
        # 有効なままで、どちらを入力すべきか分からない
        self._on_version_changed(self.version_combo.currentText())
        
        # OID入力
        oid_group = QGroupBox("OID")
        oid_layout = QVBoxLayout()
        self.oid_edit = QLineEdit()
        oid_layout.addWidget(self.oid_edit)
        oid_group.setLayout(oid_layout)
        layout.addWidget(oid_group)
        
        # ボタン
        btn_layout = QHBoxLayout()
        self.get_button = QPushButton("GET")
        self.get_button.clicked.connect(self._on_get_clicked)
        btn_layout.addWidget(self.get_button)
        self.walk_button = QPushButton("WALK")
        self.walk_button.clicked.connect(self._on_walk_clicked)
        btn_layout.addWidget(self.walk_button)
        btn_layout.addStretch()
        self.export_button = QPushButton("エクスポート")
        self.export_button.clicked.connect(self._on_export_clicked)
        btn_layout.addWidget(self.export_button)
        self.clear_button = QPushButton("クリア")
        self.clear_button.clicked.connect(self._on_clear_clicked)
        btn_layout.addWidget(self.clear_button)
        layout.addLayout(btn_layout)
        
        # 結果テーブル
        self.result_table = QTableView()
        self.result_table.setModel(self.result_model)
        self.result_table.setAlternatingRowColors(True)
        layout.addWidget(self.result_table)
        
        # ステータス
        self.status_label = QLabel("準備完了")
        layout.addWidget(self.status_label)
        
        widget.setLayout(layout)
        return widget
    
    def _create_trap_tab(self):
        """Trap受信タブの作成"""
        widget = QWidget()
        layout = QVBoxLayout()
        
        # Trap受信設定
        trap_group = QGroupBox("Trap受信設定")
        trap_layout = QGridLayout()
        trap_layout.addWidget(QLabel("受信ポート:"), 0, 0)
        self.trap_port_spinbox = QSpinBox()
        self.trap_port_spinbox.setRange(1, 65535)
        self.trap_port_spinbox.setValue(162)
        trap_layout.addWidget(self.trap_port_spinbox, 0, 1)
        trap_layout.addWidget(QLabel("バージョン:"), 1, 0)
        self.trap_version_combo = QComboBox()
        self.trap_version_combo.addItems(["両方", "v1/v2c", "v3"])
        trap_layout.addWidget(self.trap_version_combo, 1, 1)

        trap_layout.addWidget(QLabel("Community:"), 2, 0)
        self.trap_community_edit = QLineEdit("public")
        trap_layout.addWidget(self.trap_community_edit, 2, 1)

        trap_layout.addWidget(QLabel("v3 ユーザ名:"), 3, 0)
        self.trap_v3_username_edit = QLineEdit()
        trap_layout.addWidget(self.trap_v3_username_edit, 3, 1)

        trap_layout.addWidget(QLabel("v3 認証方式:"), 4, 0)
        self.trap_v3_auth_combo = QComboBox()
        for label, key in self.AUTH_PROTOCOL_CHOICES:
            self.trap_v3_auth_combo.addItem(label, key)
        trap_layout.addWidget(self.trap_v3_auth_combo, 4, 1)

        trap_layout.addWidget(QLabel("v3 認証パスワード:"), 4, 2)
        self.trap_v3_auth_password_edit = QLineEdit()
        self.trap_v3_auth_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        trap_layout.addWidget(self.trap_v3_auth_password_edit, 4, 3)

        trap_layout.addWidget(QLabel("v3 暗号方式:"), 5, 0)
        self.trap_v3_priv_combo = QComboBox()
        for label, key in self.PRIV_PROTOCOL_CHOICES:
            self.trap_v3_priv_combo.addItem(label, key)
        trap_layout.addWidget(self.trap_v3_priv_combo, 5, 1)

        trap_layout.addWidget(QLabel("v3 暗号パスワード:"), 5, 2)
        self.trap_v3_priv_password_edit = QLineEdit()
        self.trap_v3_priv_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        trap_layout.addWidget(self.trap_v3_priv_password_edit, 5, 3)

        trap_layout.addWidget(QLabel("v3 EngineID:"), 6, 0)
        self.trap_v3_engine_ids_edit = QTextEdit()
        self.trap_v3_engine_ids_edit.setFixedHeight(60)
        self.trap_v3_engine_ids_edit.setPlaceholderText("8000000001020304")
        trap_layout.addWidget(self.trap_v3_engine_ids_edit, 6, 1, 1, 3)

        trap_layout.addWidget(QLabel(
            "v3 Trap は送信元機器の EngineID を登録しないと受信できません。"
            "1行に1つ、16進で入力してください（Cisco IOS なら show snmp engineID）。"),
            7, 0, 1, 4)

        trap_group.setLayout(trap_layout)
        layout.addWidget(trap_group)
        
        # 受信の開始/停止（TFTP/FTP/SFTP サーバーと同じ見た目・配置に統一）
        recv_btn_layout = QHBoxLayout()
        self.trap_start_button = QPushButton("▶ 受信開始")
        self.trap_start_button.clicked.connect(self._on_trap_start_clicked)
        self.trap_start_button.setStyleSheet("QPushButton { background-color: #4CAF50; color: white; padding: 8px; font-weight: bold; }")
        recv_btn_layout.addWidget(self.trap_start_button)
        self.trap_stop_button = QPushButton("⬛ 受信停止")
        self.trap_stop_button.clicked.connect(self._on_trap_stop_clicked)
        self.trap_stop_button.setVisible(False)
        self.trap_stop_button.setStyleSheet("QPushButton { background-color: #f44336; color: white; padding: 8px; font-weight: bold; }")
        recv_btn_layout.addWidget(self.trap_stop_button)
        layout.addLayout(recv_btn_layout)

        # 受信状態（サーバーパネルと同じ GroupBox 形式）
        trap_status_group = QGroupBox("受信状態")
        trap_status_layout = QVBoxLayout()
        self.trap_status_label = QLabel("🔴 停止中")
        self.trap_status_label.setStyleSheet("color: #f44336; font-weight: bold; font-size: 14px;")
        trap_status_layout.addWidget(self.trap_status_label)
        trap_status_group.setLayout(trap_status_layout)
        layout.addWidget(trap_status_group)

        # 表示操作ボタン
        btn_layout = QHBoxLayout()
        self.trap_expand_button = QPushButton("すべて展開")
        self.trap_expand_button.clicked.connect(self._on_trap_expand_all_clicked)
        btn_layout.addWidget(self.trap_expand_button)
        self.trap_collapse_button = QPushButton("すべて縮小")
        self.trap_collapse_button.clicked.connect(self._on_trap_collapse_all_clicked)
        btn_layout.addWidget(self.trap_collapse_button)
        self.trap_export_button = QPushButton("エクスポート")
        self.trap_export_button.clicked.connect(self._on_trap_export_clicked)
        btn_layout.addWidget(self.trap_export_button)
        self.trap_clear_button = QPushButton("クリア")
        self.trap_clear_button.clicked.connect(self._on_trap_clear_clicked)
        btn_layout.addWidget(self.trap_clear_button)
        layout.addLayout(btn_layout)
        
        # TrapツリーView
        self.trap_tree = QTreeView()
        self.trap_tree.setModel(self.trap_tree_model)
        self.trap_tree.setAlternatingRowColors(True)
        self.trap_tree.setUniformRowHeights(False)
        self.trap_tree.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)  # 編集無効化
        self.trap_tree.setExpandsOnDoubleClick(False)  # デフォルトのダブルクリック展開を無効化
        self.trap_tree.doubleClicked.connect(self._on_trap_tree_double_clicked)  # ダブルクリックで展開/折りたたみ
        
        # ヘッダー設定
        self.trap_tree_model.setHorizontalHeaderLabels(["時刻", "送信元IP", "Trap OID / VarBind", "値"])
        header = self.trap_tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)  # 最後の列を伸縮
        
        # 初期列幅設定
        header.resizeSection(0, 150)  # 時刻
        header.resizeSection(1, 120)  # 送信元IP
        header.resizeSection(2, 200)  # Trap OID / VarBind
        # 最後の列（値）は自動伸縮
        
        layout.addWidget(self.trap_tree)
        
        widget.setLayout(layout)
        return widget
    
    def _on_version_changed(self, version: str):
        """
        バージョン選択に合わせて認証タブを切り替える

        Args:
            version: 'v1' / 'v2c' / 'v3'
        """
        titles = [self.auth_tabs.tabText(i) for i in range(self.auth_tabs.count())]
        target = "v3認証" if version == "v3" else "v1/v2c認証"
        if target in titles:
            self.auth_tabs.setCurrentIndex(titles.index(target))

        # 使わない方のタブは無効にして、どちらを入力すべきかを明示する
        for i, title in enumerate(titles):
            if title == "v1/v2c認証":
                self.auth_tabs.setTabEnabled(i, version != "v3")
            elif title == "v3認証":
                self.auth_tabs.setTabEnabled(i, version == "v3")

    def _v3_input_error(self):
        """
        v3 の入力に問題があればその説明を返す（無ければ None）

        ユーザ名が空のままだと UsmUserData('') の noAuthNoPriv になり、
        まともに設定された機器では必ず失敗する。原因が分かりにくいので
        実行前に止める。認証なしで暗号化も SNMPv3 では成立しない。
        """
        if self.version_combo.currentText() != 'v3':
            return None
        if not self.v3_username_edit.text().strip():
            return "SNMPv3 のユーザ名を入力してください。"
        if (self.v3_auth_combo.currentData() == "none"
                and self.v3_priv_combo.currentData() != "none"):
            return ("認証なしでは暗号化を使えません。\n"
                    "認証方式を選ぶか、暗号方式を「なし」にしてください。")
        return None
    
    def _collect_v3_params(self) -> dict:
        """GET/WALK 用の v3 認証パラメータを集める"""
        return {
            'username': self.v3_username_edit.text().strip(),
            'auth_protocol': self.v3_auth_combo.currentData(),
            'auth_password': self.v3_auth_password_edit.text(),
            'priv_protocol': self.v3_priv_combo.currentData(),
            'priv_password': self.v3_priv_password_edit.text(),
        }

    def _collect_trap_v3_users(self) -> list:
        """
        Trap 受信用の v3 ユーザ定義を組み立てる

        Returns:
            ユーザ定義の dict のリスト（ユーザ名が空なら空リスト）
        """
        username = self.trap_v3_username_edit.text().strip()
        if not username:
            return []

        lines = self.trap_v3_engine_ids_edit.toPlainText().split("\n")
        engine_ids = [line.strip() for line in lines if line.strip()]

        return [{
            'username': username,
            'auth_protocol': self.trap_v3_auth_combo.currentData(),
            'auth_password': self.trap_v3_auth_password_edit.text(),
            'priv_protocol': self.trap_v3_priv_combo.currentData(),
            'priv_password': self.trap_v3_priv_password_edit.text(),
            'engine_ids': engine_ids,
        }]

    def _collect_request_params(self) -> dict:
        """
        GET/WALK 共通のリクエストパラメータを組み立てる

        バージョンごとに必要なものだけを入れる。v3 に community を
        混ぜても今の core は無視するが、意味の無い値を運ぶと
        後から読む人を迷わせる。
        """
        version = self.version_combo.currentText()
        params = {'port': self.port_spinbox.value(), 'version': version}
        if version == 'v3':
            params.update(self._collect_v3_params())
        else:
            params['community'] = self.community_edit.text()
        return params
    
    def _on_preset_changed(self, preset: str):
        if "sysDescr" in preset:
            self.oid_edit.setText("1.3.6.1.2.1.1.1.0")
        elif "sysName" in preset:
            self.oid_edit.setText("1.3.6.1.2.1.1.5.0")
        elif "sysUpTime" in preset:
            self.oid_edit.setText("1.3.6.1.2.1.1.3.0")
    
    def _on_get_clicked(self):
        if not self.snmp_manager:
            QMessageBox.warning(self, "エラー", "SNMPマネージャーが設定されていません。")
            return
        host = self.host_edit.text().strip()
        if not host:
            QMessageBox.warning(self, "エラー", "ホストを入力してください。")
            return
        oids = [o.strip() for o in self.oid_edit.text().split(',')]
        v3_error = self._v3_input_error()
        if v3_error:
            QMessageBox.warning(self, "エラー", v3_error)
            return

        params = self._collect_request_params()
        self.snmp_manager.snmp_get(host, oids, **params)
        self.status_label.setText("GET実行中...")
    
    def _on_walk_clicked(self):
        if not self.snmp_manager:
            QMessageBox.warning(self, "エラー", "SNMPマネージャーが設定されていません。")
            return
        host = self.host_edit.text().strip()
        if not host:
            QMessageBox.warning(self, "エラー", "ホストを入力してください。")
            return
        oid = self.oid_edit.text().strip()
        v3_error = self._v3_input_error()
        if v3_error:
            QMessageBox.warning(self, "エラー", v3_error)
            return

        params = self._collect_request_params()
        self.snmp_manager.snmp_walk(host, oid, **params)
        self.status_label.setText("WALK実行中...")
    
    def _on_export_clicked(self):
        """GET/WALK 結果をエクスポート（Trap と同じく txt/csv/json）"""
        results = self.result_model.get_all_results()
        if not results:
            QMessageBox.information(self, "情報", "エクスポートするデータがありません。")
            return
        default_name = "snmp_result_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".txt"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "SNMP結果をエクスポート",
            default_name,
            "テキストファイル (*.txt);;CSVファイル (*.csv);;JSONファイル (*.json)"
        )
        if not file_path:
            return
        try:
            if file_path.endswith(".csv"):
                self._export_results_to_csv(file_path, results)
            elif file_path.endswith(".json"):
                self._export_results_to_json(file_path, results)
            else:
                self._export_results_to_txt(file_path, results)
            QMessageBox.information(self, "成功", "SNMP結果をエクスポートしました:\n" + file_path)
        except Exception as e:
            QMessageBox.critical(self, "エラー", "エクスポート中にエラーが発生しました:\n" + str(e))

    def _export_results_to_csv(self, file_path: str, results):
        """CSV形式で GET/WALK 結果を書き出す"""
        import csv
        with open(file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["OID", "Type", "Value"])
            for row in results:
                writer.writerow(list(row))

    def _export_results_to_json(self, file_path: str, results):
        """JSON形式で GET/WALK 結果を書き出す"""
        import json
        data = {
            "exported_at": datetime.now().isoformat(),
            "host": self.host_edit.text(),
            "count": len(results),
            "results": [
                {"oid": row[0], "type": row[1], "value": row[2]} for row in results
            ],
        }
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _export_results_to_txt(self, file_path: str, results):
        """テキスト形式で GET/WALK 結果を書き出す"""
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("SNMP GET/WALK 結果\n")
            f.write("エクスポート日時: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n")
            f.write("対象ホスト: " + self.host_edit.text() + "\n")
            f.write("件数: " + str(len(results)) + "\n")
            f.write("=" * 60 + "\n")
            for row in results:
                f.write("OID  : " + str(row[0]) + "\n")
                f.write("Type : " + str(row[1]) + "\n")
                f.write("Value: " + str(row[2]) + "\n")
                f.write("-" * 60 + "\n")

    def _on_clear_clicked(self):
        self.result_model.clear_results()
        self.status_label.setText("結果をクリアしました")
    
    def _on_trap_start_clicked(self):
        if not self.snmp_manager:
            QMessageBox.warning(self, "エラー", "SNMPマネージャーが設定されていません。")
            return
        
        # MIB読み込み中の場合は待機
        if self.mib_loading and not self.mib_loaded:
            QMessageBox.information(
                self, 
                "MIB読み込み中", 
                "MIBをバックグラウンドで読み込み中です。\n完了までお待ちください。"
            )
            return
        
        # Trap受信を開始
        port = self.trap_port_spinbox.value()
        version = self.trap_version_combo.currentText()

        communities = [] if version == "v3" else [self.trap_community_edit.text()]
        v3_users = [] if version == "v1/v2c" else self._collect_trap_v3_users()

        # 起動できなければ表示を変えない（エラーは error_occurred で通知済み）
        if not self.snmp_manager.start_trap_receiver(port, communities, v3_users):
            return
        self.trap_start_button.setVisible(False)
        self.trap_stop_button.setVisible(True)
        self.trap_status_label.setText(f"🔵 受信中 (ポート {port})")
        self.trap_status_label.setStyleSheet("color: #2196F3; font-weight: bold; font-size: 14px;")
    
    def _start_background_mib_loading(self):
        """バックグラウンドでMIB読み込みを開始"""
        if self.mib_loaded or self.mib_loading:
            return
        
        self.mib_loading = True
        print("[SNMPPanel] バックグラウンドでMIB読み込みを開始...")
        
        # MIB読み込みスレッド作成
        self.mib_thread = MIBLoaderThread()
        self.mib_thread.finished_signal.connect(self._on_background_mib_load_finished)
        self.mib_thread.start()
    
    def _on_background_mib_load_finished(self, success: bool):
        """バックグラウンドMIB読み込み完了時の処理"""
        self.mib_loading = False
        self.mib_loaded = True
        
        if success:
            print("[SNMPPanel] バックグラウンドMIB読み込み完了")
        else:
            print("[SNMPPanel] バックグラウンドMIB読み込みエラー")
    
    
    def _on_trap_stop_clicked(self):
        if self.snmp_manager:
            self.snmp_manager.stop_trap_receiver()
        self.trap_start_button.setVisible(True)
        self.trap_stop_button.setVisible(False)
        self.trap_status_label.setText("🔴 停止中")
        self.trap_status_label.setStyleSheet("color: #f44336; font-weight: bold; font-size: 14px;")
    
    def _on_trap_clear_clicked(self):
        self.trap_tree_model.clear()
        self.trap_tree_model.setHorizontalHeaderLabels(["時刻", "送信元IP", "Trap OID / VarBind", "値"])
        self.trap_data_list.clear()
    
    def _on_trap_expand_all_clicked(self):
        """すべてのTrapを展開"""
        self.trap_tree.expandAll()
    
    def _on_trap_collapse_all_clicked(self):
        """すべてのTrapを縮小"""
        self.trap_tree.collapseAll()
    
    def _on_trap_tree_double_clicked(self, index: QModelIndex):
        """ツリーアイテムがダブルクリックされた時の処理（どの列でも展開/折りたたみ）"""
        # ダブルクリックされた行の第0列のindexを取得（展開/折りたたみは第0列を基準にする）
        first_column_index = index.sibling(index.row(), 0)
        
        if self.trap_tree.isExpanded(first_column_index):
            self.trap_tree.collapse(first_column_index)
        else:
            self.trap_tree.expand(first_column_index)
    
    def _on_trap_export_clicked(self):
        """Trapログをエクスポート"""
        if not self.trap_data_list:
            QMessageBox.information(self, "情報", "エクスポートするデータがありません。")
            return
        
        # ファイル保存ダイアログ
        file_path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Trapログをエクスポート",
            f"trap_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "テキストファイル (*.txt);;CSVファイル (*.csv);;JSONファイル (*.json)"
        )
        
        if not file_path:
            return
        
        try:
            # ファイル拡張子で形式を判定
            if file_path.endswith('.csv'):
                self._export_to_csv(file_path)
            elif file_path.endswith('.json'):
                self._export_to_json(file_path)
            else:  # .txt or other
                self._export_to_txt(file_path)
            
            QMessageBox.information(self, "成功", f"Trapログをエクスポートしました:\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"エクスポート中にエラーが発生しました:\n{str(e)}")
    
    def _export_to_csv(self, file_path: str):
        """CSV形式でエクスポート"""
        import csv
        
        with open(file_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            
            # ヘッダー
            writer.writerow(['時刻', '送信元IP', 'Trap OID', 'VarBind OID', 'VarBind 値'])
            
            # データ（新しい順＝trap_data_listの順）
            for trap in self.trap_data_list:
                timestamp = trap['timestamp']
                source_ip = trap['source_ip']
                trap_oid = trap['trap_oid']
                varbinds = trap['varbinds']
                
                # VarBindsがある場合は各VarBindを1行として出力
                if varbinds:
                    for vb in varbinds:
                        writer.writerow([
                            timestamp,
                            source_ip,
                            trap_oid,
                            vb['oid'],
                            vb['value']
                        ])
                else:
                    # VarBindsがない場合は1行だけ出力
                    writer.writerow([timestamp, source_ip, trap_oid, '', ''])
    
    def _export_to_json(self, file_path: str):
        """JSON形式でエクスポート"""
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(self.trap_data_list, f, indent=2, ensure_ascii=False)
    
    def _export_to_txt(self, file_path: str):
        """テキスト形式でエクスポート（人間が読みやすい形式）"""
        resolver = get_resolver()
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write("=" * 80 + "\n")
            f.write("SNMP Trap Log\n")
            f.write(f"エクスポート日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"総Trap数: {len(self.trap_data_list)}\n")
            f.write("=" * 80 + "\n\n")
            
            # データ（新しい順＝trap_data_listの順）
            for i, trap in enumerate(self.trap_data_list, 1):
                timestamp = trap['timestamp']
                source_ip = trap['source_ip']
                trap_oid = trap['trap_oid']
                varbinds = trap['varbinds']
                
                # Trap OIDを名前に変換
                trap_name = resolver.resolve_oid(trap_oid) if trap_oid else 'N/A'
                
                f.write(f"[{i}] Trap受信\n")
                f.write(f"  時刻: {timestamp}\n")
                f.write(f"  送信元IP: {source_ip}\n")
                f.write(f"  Trap OID: {trap_name}\n")
                f.write(f"            ({trap_oid})\n")
                
                if varbinds:
                    f.write(f"  VarBinds: {len(varbinds)}個\n")
                    for j, vb in enumerate(varbinds, 1):
                        oid = vb['oid']
                        value = vb['value']
                        oid_name = resolver.resolve_oid(oid)
                        
                        f.write(f"    [{j}] {oid_name}\n")
                        if oid_name != oid:
                            f.write(f"        OID: {oid}\n")
                        f.write(f"        値: {value}\n")
                else:
                    f.write(f"  VarBinds: なし\n")
                
                f.write("\n" + "-" * 80 + "\n\n")
    
    def set_snmp_manager(self, manager):
        self.snmp_manager = manager
        if self.snmp_manager:
            self.snmp_manager.operation_completed.connect(self._on_operation_completed)
            self.snmp_manager.trap_received.connect(self._on_trap_received)
            self.snmp_manager.trap_receiver_started.connect(self._on_trap_receiver_started)
            self.snmp_manager.trap_receiver_stopped.connect(self._on_trap_receiver_stopped)
            self.snmp_manager.error_occurred.connect(self._on_error_occurred)
    
    def _on_operation_completed(self, success: bool, result):
        if success:
            self.result_model.set_results(result)
            self.status_label.setText(f"完了: {len(result)}件")
        else:
            QMessageBox.critical(self, "エラー", str(result))
            self.status_label.setText("エラー")
    
    def _on_trap_received(self, trap_data: dict):
        self._add_trap_to_tree(trap_data)
    
    def _add_trap_to_tree(self, trap_data: dict):
        """TrapデータをツリーViewに追加"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        source_ip = trap_data.get('source_ip', '')
        trap_oid = trap_data.get('trap_oid', 'N/A')
        varbinds = trap_data.get('varbinds', [])
        
        # 完全なデータを保存（エクスポート用）
        self.trap_data_list.insert(0, {
            'timestamp': timestamp,
            'source_ip': source_ip,
            'trap_oid': trap_oid,
            'varbinds': varbinds
        })
        
        # Trap OIDを名前に変換
        resolver = get_resolver()
        trap_name = resolver.resolve_oid(trap_oid) if trap_oid else 'N/A'
        
        # 標準的なVarBinds（sysUpTime, snmpTrapOID）を除外
        filtered_vbs = [vb for vb in varbinds if vb['oid'] not in ['1.3.6.1.2.1.1.3.0', '1.3.6.1.6.3.1.1.4.1.0']]
        
        # 親アイテム作成（Trap情報）
        timestamp_item = QStandardItem(timestamp)
        source_ip_item = QStandardItem(source_ip)
        trap_oid_item = QStandardItem(trap_name)
        varbinds_count = f"{len(filtered_vbs)} VarBinds" if filtered_vbs else "VarBindsなし"
        value_item = QStandardItem(varbinds_count)
        
        # 親行をツリーのルートに挿入（最新を先頭に）
        self.trap_tree_model.insertRow(0, [timestamp_item, source_ip_item, trap_oid_item, value_item])
        
        # 各VarBindを子アイテムとして追加
        for vb in filtered_vbs:
            oid = vb['oid']
            value = vb['value']
            
            # OIDを名前に変換
            oid_name = resolver.resolve_oid(oid)
            
            # 子アイテム作成（1行につき1つのVarBind）
            child_timestamp = QStandardItem("")  # 空
            child_source = QStandardItem("")  # 空
            child_oid = QStandardItem(oid_name)
            child_value = QStandardItem(value)
            
            # 親の最初の列に子行を追加
            timestamp_item.appendRow([child_timestamp, child_source, child_oid, child_value])
    
    def _on_trap_receiver_started(self):
        """Trap受信開始時の処理"""
        print("[SNMPPanel] Trap受信が正常に開始されました")
    
    def _on_trap_receiver_stopped(self):
        """Trap受信停止時の処理"""
        print("[SNMPPanel] Trap受信が正常に停止しました")
    
    def _on_error_occurred(self, error: str):
        QMessageBox.warning(self, "警告", error)