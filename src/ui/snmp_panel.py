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
from contextlib import contextmanager
from datetime import datetime
import json
import os
import re
import tempfile
from core.mib_resolver import get_resolver, MIBResolver
from core.snmp_manager import v3_password_error


@contextmanager
def atomic_text_write(file_path: str, **open_kwargs):
    """書き切れた時だけ保存先を置き換える open('w') の代わり

    保存先を直接開くと、開いた時点で元の内容が消える。書き込みの途中で
    失敗（ディスク満杯・共有断）すると新しい内容も書き切れないので、
    利用者は新旧どちらも失う。同じディレクトリの一時ファイルへ書き切って
    から os.replace() で差し替え、失敗したら一時ファイルだけ捨てる。

    open_kwargs は open() にそのまま渡す（encoding / newline など）。
    """
    directory = os.path.dirname(os.path.abspath(file_path))
    fd, tmp_path = tempfile.mkstemp(dir=directory,
                                    prefix=".netbelt-export-", suffix=".tmp")
    os.close(fd)
    try:
        with open(tmp_path, "w", **open_kwargs) as f:
            yield f
        # 同じディレクトリなので、置き換えは失敗しても中途半端にはならない
        os.replace(tmp_path, file_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


# pysnmp が渡す securityModel / securityLevel の値
_SECURITY_MODELS = {'1': 'v1', '2': 'v2c', '3': 'v3'}
_SECURITY_LEVELS = {'1': 'noAuthNoPriv', '2': 'authNoPriv', '3': 'authPriv'}


def describe_trap_security(trap_data: dict) -> str:
    """Trap がどの版・どの保護レベルで届いたかを1行で表す

    v1/v2c の security_name は受信側が内部で振った名前であって
    コミュニティそのものではないので出さない（コミュニティは実質的な
    認証情報なので、表示にもエクスポートにも載せない方針）。
    v3 のユーザ名は秘密ではないため出す。
    """
    model = _SECURITY_MODELS.get(str(trap_data.get('security_model', '')), '')
    if model != 'v3':
        return model

    level = _SECURITY_LEVELS.get(str(trap_data.get('security_level', '')), '')
    text = f'v3 {level}'.strip()
    name = trap_data.get('security_name', '')
    return f'{text} / {name}' if name else text


class SNMPResultTableModel(QAbstractTableModel):
    """SNMP GET/WALK結果テーブルモデル"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
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

    # 保持する Trap の件数。上限が無いと、受信を張りっぱなしにする常用で
    # メモリが単調に増え続ける（VarBind 3件の Trap あたり約 12KB、
    # 100,000 件で約 1.2GB。クリアするまで解放されない）。
    # Syslog パネルの max_messages と同じ考え方・同じ既定値にしてある。
    DEFAULT_MAX_TRAPS = 1000

    # 表計算ソフトがセルを数式として読み始める先頭文字。
    _CSV_FORMULA_STARTERS = "=+-@"
    # そのまま数値として書いてよい形。float() で判定すると -inf / +nan /
    # -1_000 まで「数」になるが、表計算は先頭の - や + を見て数式として
    # 解釈するので、通してはいけない。
    _CSV_PLAIN_NUMBER = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")

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
        
        # 親を持たせる（setModel は所有権を取らない。sftp_panel の注記を参照）
        self.result_model = SNMPResultTableModel(self)
        self.trap_tree_model = QStandardItemModel(self)
        self.trap_data_list = []  # 完全なTrapデータ（エクスポート用）
        self.max_traps = self._configured_max_traps()
        # WALK が途中で途切れたときの理由。結果より先に届き、結果を
        # 表示するときに使って忘れる
        self._partial_reason = None
        # 直近の結果が途中までだった理由。書き出しに添えるため、次の完走か
        # クリアまで持ち続ける
        self._last_partial_reason = None
        # 要求を出した時点のホストと、いま表の結果を取得したホスト。書き出しは
        # 後者を使う。保存時の入力欄を使うと、A の結果が B の記録になる
        self._request_host = ""
        self._result_host = ""
        
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
        # GET/WALK の実行中だけ出す（Trap 受信の停止ボタンと同じ出し方）。
        # GET・WALK は押せるままにして、実行中の要求はマネージャが断る
        self.stop_button = QPushButton("停止")
        self.stop_button.clicked.connect(self._on_stop_clicked)
        self.stop_button.setVisible(False)
        btn_layout.addWidget(self.stop_button)
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

        # 手動FW許可（3CDaemon方式で通らない時の復旧用・押した時だけ管理者昇格/UAC）
        fw_layout = QHBoxLayout()
        self.fw_allow_btn = QPushButton("ファイアウォールで許可（管理者）")
        self.fw_allow_btn.setToolTip("Trap が届かない場合に押してください。Windowsファイアウォールの受信許可を追加します（管理者昇格/UACが1回出ます）。")
        self.fw_allow_btn.clicked.connect(self._on_fw_allow)
        fw_layout.addWidget(self.fw_allow_btn)
        fw_layout.addStretch()
        layout.addLayout(fw_layout)

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
        self.trap_tree_model.setHorizontalHeaderLabels(
            ["時刻", "送信元IP", "セキュリティ", "Trap OID / VarBind", "値"])
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
        return v3_password_error(
            self.v3_auth_combo.currentData(), self.v3_auth_password_edit.text(),
            self.v3_priv_combo.currentData(), self.v3_priv_password_edit.text())
    
    def _collect_v3_params(self) -> dict:
        """GET/WALK 用の v3 認証パラメータを集める"""
        return {
            'username': self.v3_username_edit.text().strip(),
            'auth_protocol': self.v3_auth_combo.currentData(),
            'auth_password': self.v3_auth_password_edit.text(),
            'priv_protocol': self.v3_priv_combo.currentData(),
            'priv_password': self.v3_priv_password_edit.text(),
        }

    def _trap_v3_input_error(self):
        """
        Trap 受信の v3 入力に問題があればその説明を返す（無ければ None）

        v3 Trap は送信元機器の EngineID を登録しないと1件も受信できない
        （実測で確認済み）。黙って受信を開始すると「起動したのに何も
        来ない」という原因の分かりにくい状態になるため、実行前に止める。
        認証なしでの暗号化も SNMPv3 では成立しない。
        """
        version = self.trap_version_combo.currentText()
        if version == 'v1/v2c':
            return None

        # v3 を実際に使うかどうかはユーザ名の有無で決まる。ここを先に
        # 決めないと、v3 を使うつもりの無い「両方」の利用者が、暗号方式を
        # 触っただけで受信を始められなくなる。
        username = self.trap_v3_username_edit.text().strip()
        if not username:
            if version == 'v3':
                return ("v3 を選んだ場合は、v3 のユーザ名を入力してください。\n"
                        "v1/v2c だけを受けるならバージョンを「v1/v2c」にしてください。")
            return None

        engine_ids = self._collect_trap_v3_users()[0]['engine_ids']
        if not engine_ids:
            return ("v3 Trap を受信するには、送信元機器の EngineID を"
                    "1行に1つ登録してください。")

        # 16進として読めない値は core で例外になり、bind 失敗としてしか
        # 伝わらない。ここで具体的に指摘する。
        for engine_id in engine_ids:
            if len(engine_id) % 2 or not all(c in "0123456789abcdefABCDEF"
                                             for c in engine_id):
                return ("EngineID は偶数桁の16進で入力してください。\n"
                        f"読めない値: {engine_id}")

        if (self.trap_v3_auth_combo.currentData() == "none"
                and self.trap_v3_priv_combo.currentData() != "none"):
            return ("認証なしでは暗号化を使えません。\n"
                    "認証方式を選ぶか、暗号方式を「なし」にしてください。")
        return v3_password_error(
            self.trap_v3_auth_combo.currentData(),
            self.trap_v3_auth_password_edit.text(),
            self.trap_v3_priv_combo.currentData(),
            self.trap_v3_priv_password_edit.text())

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
        # 受理された要求のホストだけを記録する。実行中で断られた要求の
        # ホストまで記録すると、いま走っている要求の結果に別のホストが付く。
        # 断られたら表示にも触れない。断りの警告を開いている間に前の操作の
        # 完了が届くので、閉じた後に「実行中」で上書きすることになる
        if not self.snmp_manager.snmp_get(host, oids, **params):
            return
        self._request_host = host
        self.status_label.setText("GET実行中...")
        self._show_stop_button(True)
    
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
        # 受理された要求のホストだけを記録し、断られたら表示にも触れない
        # （GET と同じ理由）
        if not self.snmp_manager.snmp_walk(host, oid, **params):
            return
        self._request_host = host
        self.status_label.setText("WALK実行中...")
        self._show_stop_button(True)

    def _show_stop_button(self, running: bool):
        """停止ボタンを、GET/WALK の実行中だけ押せる状態で出す"""
        self.stop_button.setVisible(running)
        self.stop_button.setEnabled(True)

    def _on_stop_clicked(self):
        """実行中の GET/WALK を止める

        待っている応答は切れないので、取り消しは次の応答を受けたところで
        効く（応答しない機器では最大約 6 秒）。それまでは「停止中…」と出し、
        新しい要求は実行中と同じくマネージャが断る。止まると、そこまでに
        取れた行が operation_cancelled で届く。
        """
        if not self.snmp_manager or not self.snmp_manager.request_cancel():
            return   # 結果が既に届く途中。そのまま完了として表示される
        self.stop_button.setEnabled(False)
        self.status_label.setText("停止中…（次の応答を待ってから止まります）")

    def _refuse_if_recording(self, title: str, file_path: str) -> bool:
        """保存先が端末のログ記録に使われていたら断る（断ったら True）

        書き出しは保存先を別の内容へ作り直す（atomic_text_write が一時ファイル
        へ書き切ってから os.replace で置き換える）。記録中のファイルを選ばれる
        と、置き換えが通れば記録済みの内容は失われ、端末は開いたままのハンドル
        で自分のオフセットから書き続けるので、双方のファイルが壊れる。
        """
        from core import log_recording
        device_name = log_recording.device_using(file_path)
        if device_name is None:
            return False
        QMessageBox.warning(
            self, title,
            "このファイルは %s のログ記録に使用中です:\n%s\n"
            "別のファイルを選ぶか、先にそのログ記録を停止してください。"
            % (device_name, file_path))
        return True

    @staticmethod
    def _export_error_text(error: Exception, file_path: str) -> str:
        """エクスポート失敗の知らせ文を作る（使用中なら言い換える）

        書き出しは一時ファイルへ書き切ってから os.replace() で保存先を
        置き換える（atomic_text_write）。Windows では保存先を別のプログラム
        （ビューア等）が開いたままだと、この置き換えが PermissionError に
        なる。元の内容は残り一時ファイルも消えるので壊れはしないが、
        既定の文には一時ファイル名しか出ないので、開いているものを閉じれば
        通ると利用者に分からない。使用中のときだけ、保存先と対処を出す。

        使用中以外の失敗は従来どおりそのまま見せる。
        """
        if isinstance(error, PermissionError):
            return ("このファイルは別のプログラムが開いているか、"
                    "書き込みが許可されていません:\n%s\n"
                    "開いているプログラムを閉じてから、もう一度試してください。"
                    % file_path)
        return "エクスポート中にエラーが発生しました:\n" + str(error)

    @staticmethod
    def _export_format(file_path: str) -> str:
        """保存先の拡張子から書き出す形式を決める（"csv" / "json" / "txt"）

        大文字小文字は区別しない。区別すると out.CSV が TXT の中身で
        書かれたうえ「エクスポートしました」と成功扱いになり、中身と
        拡張子の食い違ったファイルが残る。ダイアログは選んだフィルタの
        拡張子を小文字で補うので普段は当たるが、利用者が自分で .CSV と
        打った場合と、大文字名の既存ファイルを選び直した場合に外れる。

        当てはまらない拡張子は従来どおり TXT（既定の形式）。
        """
        ext = os.path.splitext(file_path)[1].lower()
        if ext in ('.csv', '.json'):
            return ext[1:]
        return 'txt'

    def _on_export_clicked(self):
        """GET/WALK 結果をエクスポート（Trap と同じく txt/csv/json）"""
        # 行・ホスト・途中までの理由は、ダイアログを開く前にまとめて固定し、
        # 書き出しへ引数で渡す。モーダルダイアログはネストしたイベント
        # ループで queued シグナルを処理するので、開いている間に次の WALK が
        # 完走すると self は次の結果に変わる。行だけ先に取ってホストと理由を
        # 後から self で読むと、前の途中までの行に「完走」と次のホストが付く
        results = self.result_model.get_all_results()
        host = self._result_host
        reason = self._last_partial_reason
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
        if self._refuse_if_recording("SNMP結果をエクスポート", file_path):
            return
        try:
            # 拡張子は大文字小文字を区別せずに見る。区別すると out.CSV が
            # TXT の中身で書かれ、しかも「成功」と出る（理由は
            # _export_format と同じ）
            fmt = self._export_format(file_path)
            if fmt == "csv":
                self._export_results_to_csv(file_path, results, host, reason)
            elif fmt == "json":
                self._export_results_to_json(file_path, results, host, reason)
            else:
                self._export_results_to_txt(file_path, results, host, reason)
            QMessageBox.information(self, "成功", "SNMP結果をエクスポートしました:\n" + file_path)
        except Exception as e:
            QMessageBox.critical(self, "エラー",
                                 self._export_error_text(e, file_path))

    def _configured_max_traps(self) -> int:
        """settings.snmp.max_traps を読む（壊れていれば既定値）

        config.json は手で編集できるので、数でない値や 0 以下が来る。
        そのまま使うと上限が消えたり、1件も残らなくなる。
        """
        try:
            settings = self.config_manager.config.get("settings", {})
            value = int(settings.get("snmp", {}).get(
                "max_traps", self.DEFAULT_MAX_TRAPS))
        except (AttributeError, TypeError, ValueError):
            return self.DEFAULT_MAX_TRAPS
        return value if value > 0 else self.DEFAULT_MAX_TRAPS

    def _trim_traps(self):
        """上限を超えたぶんの古い Trap を捨てる

        新しいものを先頭へ挿しているので、余るのは末尾。表示行と保存
        データを同じ数だけ削り、エクスポートの中身と画面が食い違わない
        ようにする。

        制限: 上限が効くのは、GUI が Trap を1件受け取って表示へ入れた
        後だけ。受信スレッドは1件ごとに完成した dict を queued シグナル
        で送るので、GUI が止まっている間そのキューは上限と無関係に
        伸びる（実測: 1件あたり約 2.3 KB、5万件で RSS +116 MB。GUI が
        処理し終えると解放される）。定常状態では問題にならない。受信側の
        復号が約 1,670 件/秒、GUI 側の処理が約 2,550 件/秒で、GUI の方が
        速いため未処理は常に3件以下だった（毎秒 3,000 件を外から送った
        実測でも同じ）。効くのは終了時の wait などで GUI が数十秒
        止まっている間だけなので、まとめ配送（deque + QTimer）は
        入れていない。
        """
        while len(self.trap_data_list) > self.max_traps:
            self.trap_data_list.pop()
        while self.trap_tree_model.rowCount() > self.max_traps:
            self.trap_tree_model.removeRow(self.trap_tree_model.rowCount() - 1)

    @staticmethod
    def _csv_safe(value):
        """表計算ソフトが数式として解釈しうる値を、文字列として書き出す

        csv モジュールは区切り文字と引用符しかエスケープしない。先頭が
        = + - @ の値はそのまま残り、Excel / LibreOffice で開いた瞬間に
        数式（DDE を含む）として評価される。

        値は攻撃者が選べる。v1/v2c ならコミュニティ名を知っている者が
        UDP パケット1発で任意の VarBind を入れられるし、GET/WALK 側も
        sysName / sysLocation / sysContact など機器側で自由に書ける
        文字列が入る。エクスポートは障害チケットや報告書へ回る前提なので、
        受け取った側の端末で発火する。

        負の数まで文字列にすると表として読みにくくなるので、数として
        読める値はそのまま通す。
        """
        text = "" if value is None else str(value)
        if not text:
            return text

        # 表計算ソフトは前置きの空白を落として解釈することがあるので、
        # 判定も落としてから行う（空白を1つ置くだけで抜けられてしまう）。
        stripped = text.lstrip()
        if not stripped or stripped[0] not in SNMPPanel._CSV_FORMULA_STARTERS:
            return text

        # 数として読める値はそのまま通す。ただし判定を float() に任せると
        # -inf / +nan / -1_000 まで通ってしまう。Python が数として読めても、
        # 表計算は先頭の - や + を見て数式として解釈する（-inf なら #NAME?）。
        if SNMPPanel._CSV_PLAIN_NUMBER.match(stripped):
            return text
        return "'" + text

    def _export_results_to_csv(self, file_path: str, results, host: str,
                               reason):
        """CSV形式で GET/WALK 結果を書き出す

        host / reason は呼び出し側が結果と同時に固定した値。ここで self を
        読むと、ダイアログを開いている間に届いた次の結果のものになる
        """
        import csv
        # BOM 付き（utf-8-sig）。日本語版 Excel は BOM の無い UTF-8 の CSV を
        # cp932 として開くため、見出しも機器から来た日本語も文字化けする
        with atomic_text_write(file_path, newline="", encoding="utf-8-sig") as f:
            if reason:
                # 途中までの結果であることを、見出しの前に残す
                f.write("# 途中まで: %s のため中断。全部ではありません\n" % reason)
            if host:
                # どの機器から採った結果かを、見出しの前に残す。JSON の
                # "host"・TXT の「対象ホスト:」に当たるものが CSV だけ
                # 抜けていて、ファイルを並べると取り違えても気づけなかった
                f.write("# 対象ホスト: %s\n" % host)
            writer = csv.writer(f)
            writer.writerow(["OID", "Type", "Value"])
            for row in results:
                writer.writerow([self._csv_safe(cell) for cell in row])

    def _export_results_to_json(self, file_path: str, results, host: str,
                                reason):
        """JSON形式で GET/WALK 結果を書き出す（host / reason は CSV と同じ）"""
        import json
        data = {
            "exported_at": datetime.now().isoformat(),
            "host": host,
            "count": len(results),
            # 途中までの結果かどうか。機械で読む側が見落とさないよう明示する
            "complete": reason is None,
            "partial_reason": reason,
            "results": [
                {"oid": row[0], "type": row[1], "value": row[2]} for row in results
            ],
        }
        with atomic_text_write(file_path, encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _export_results_to_txt(self, file_path: str, results, host: str,
                               reason):
        """テキスト形式で GET/WALK 結果を書き出す（host / reason は CSV と同じ）"""
        with atomic_text_write(file_path, encoding="utf-8") as f:
            f.write("SNMP GET/WALK 結果\n")
            f.write("エクスポート日時: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n")
            f.write("対象ホスト: " + host + "\n")
            f.write("件数: " + str(len(results)) + "\n")
            if reason:
                f.write("注意: 途中まで（%s のため中断。全部ではありません）\n" % reason)
            f.write("=" * 60 + "\n")
            for row in results:
                f.write("OID  : " + str(row[0]) + "\n")
                f.write("Type : " + str(row[1]) + "\n")
                f.write("Value: " + str(row[2]) + "\n")
                f.write("-" * 60 + "\n")

    def _on_clear_clicked(self):
        self.result_model.clear_results()
        self._last_partial_reason = None
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

        v3_error = self._trap_v3_input_error()
        if v3_error:
            QMessageBox.warning(self, "エラー", v3_error)
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
    
    # MIB 読み込みスレッドの終了を待つ上限（ミリ秒）。mibs/ が空なら
    # 数 ms で終わるが、ベンダー MIB を入れた初回は秒単位かかる
    MIB_LOADER_WAIT_MS = 5000

    def wait_for_background_work(self):
        """バックグラウンドの MIB 読み込みが終わるのを待つ。

        ウィンドウを閉じるときに呼ぶ。起動直後に閉じると読み込みが
        まだ走っていることがあり、待たないとプロセスの終了が先に来て
        読み込みが途中で切られる（実測: 待ちを外すと app.exec() が戻って
        から 1.01 秒でプロセスが終わり、6 秒かかる読み込みの後半は
        走らなかった）。読み込みの最後は mib_resolver が mib_cache.json を
        open('w') で書き直す処理で、一時ファイル経由ではないので、そこで
        切られれば書きかけのキャッシュが残る。読む側は except で握って
        作り直すため落ちはせず、次の起動で MIB を解析し直すだけになる。
        待ちが実際に買っているのはここまで。

        取り消し: ここには以前「待たずにパネルが破棄されると、実行中の
        QThread の破棄で Qt が abort するか、終わったスレッドの
        finished_signal が解放済みのパネルへ届いて落ちる」と書いてあったが、
        再現しないので落とした。実測（いずれも offscreen）:
          - 待ちを空実装にして MainWindow を閉じ、del して gc しても、
            実行中の QThread は破棄されない。3 周とも終了コード 0・
            stderr 空（PyQt6 が実行中の QThread への参照を保つ）。
          - main() と同じく app.exec() を回し、6 秒かかる読み込みを残した
            まま終了しても、待つ／待たないの両方で 3 回とも終了コード 0、
            "QThread: Destroyed while thread is still running" も無し。
          - 受け手の C++ 側を消してから finished_signal を出しても、PyQt が
            接続を外すのでスロットは呼ばれない。加えて
            _on_background_mib_load_finished はフラグ 2 つと print だけで、
            ウィジェットには触れない。
        テストスイートを落としていた間欠 segfault の本当の原因は、
        SNMPManager が connect(signal.emit) でシグナルを中継していたこと
        だった（47ecbde）。

        制限: 待ちは MIB_LOADER_WAIT_MS が上限で、戻り値は見ていない。
        上限を過ぎたら待つのをやめ、読み込み中のまま閉じる処理を続ける。
        無期限に待つと、応答しない MIB を掘っているあいだアプリを
        閉じられなくなるため。

        実測: mibs/ に MIB を置いていなければ読み込みは数ミリ秒で終わる
        （get_resolver の cold が 0.012 s）。ただし解析時間は mibs/ の
        総量にほぼ比例し、ベンダー MIB 一式を入れた初回（キャッシュ無効時）
        は 22MB・80 ファイルで約 5〜6 秒、44MB・150 ファイルで約 9〜12 秒
        （合成 MIB での計測）。つまりこの上限は現実に超える。超えるのは
        初回と、MIB を足したとき・MIB_PARSER_VERSION を上げたときだけで、
        以後は mib_cache.json が効いて数ミリ秒に戻る。

        超えても落ちない。遅延を 5.22〜5.6 秒に伸ばした 13 回の実行は
        いずれも終了コード 0・stderr 空で、8 秒かかるスレッドを残した
        まま閉じても "QThread: Destroyed while thread is still running"
        は出なかった（PyQt6 が実行中の QThread への参照を保持するため、
        パネルが破棄されてもスレッド側は破棄されない）。解析の途中で
        終了してもキャッシュは書かれないだけで、次回また作り直される。

        利用者に見える影響は、閉じる操作が最大でこの上限ぶん固まること。
        SNMPManager.cancel_operation も同じだけ待つので、応答しない機器への
        GET/WALK（既定で約 6 秒かかり、5 秒の待ちを実際に超える。閉じた
        ポートへの GET が実測 6.09 秒）と MIB 読み込みが重なると最大
        10 秒になる。短くする手は 2 つある。GET/WALK 側のタイムアウトを
        5 秒以内へ明示するか、上限値（ここと cancel_operation の 5000）を
        下げるか。上限を超えても異常終了しないことは上のとおり測ってある
        ので、後者も安全に取れる。5000 のままにしているのは、通常の
        読み込みは数ミリ秒で終わって固まりが見えず、遅い機器のときだけ
        効くこの値を、実機での計測なしに動かしたくないため。
        """
        thread = getattr(self, "mib_thread", None)
        if thread is not None and thread.isRunning():
            thread.wait(self.MIB_LOADER_WAIT_MS)

    def _on_background_mib_load_finished(self, success: bool):
        """バックグラウンドMIB読み込み完了時の処理"""
        self.mib_loading = False
        self.mib_loaded = True
        
        if success:
            print("[SNMPPanel] バックグラウンドMIB読み込み完了")
        else:
            print("[SNMPPanel] バックグラウンドMIB読み込みエラー")
    
    
    def _on_trap_stop_clicked(self):
        # 開始ボタンはここでは戻さない。停止要求から実際の終了までは
        # 間があり、待ち受けポートを掴んだままのスレッドが残っている
        # 状態で開始すると bind に失敗する。終了は stopped で分かる。
        self.trap_stop_button.setEnabled(False)
        self.trap_status_label.setText("⏳ 停止しています...")
        self.trap_status_label.setStyleSheet(
            "color: #ff9800; font-weight: bold; font-size: 14px;")
        if self.snmp_manager:
            self.snmp_manager.stop_trap_receiver()

    def _show_trap_stopped(self):
        """受信していない状態の表示に戻す

        受信スレッドが実際に終わったときに呼ぶ。利用者が止めた場合も、
        スレッドが自分で死んだ場合も、run() の finally から出る stopped が
        ここへ来る。片方だけだと、受信が死んでいるのに表示が残る。
        """
        self.trap_start_button.setVisible(True)
        self.trap_stop_button.setVisible(False)
        self.trap_stop_button.setEnabled(True)
        self.trap_status_label.setText("🔴 停止中")
        self.trap_status_label.setStyleSheet("color: #f44336; font-weight: bold; font-size: 14px;")
    
    def _on_trap_clear_clicked(self):
        self.trap_tree_model.clear()
        self.trap_tree_model.setHorizontalHeaderLabels(
            ["時刻", "送信元IP", "セキュリティ", "Trap OID / VarBind", "値"])
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
        # 保存する一覧は、ダイアログを開く前に固定して書き出しへ渡す
        # （GET/WALK 側と同じ理由）。モーダルダイアログはネストした
        # イベントループで queued シグナルを処理するので、開いている間に
        # 届いた Trap が「今見えているものを保存した」はずのファイルへ
        # 入り、max_traps の切り詰めで押した時点の最古の Trap が消える
        traps = list(self.trap_data_list)
        if not traps:
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

        if self._refuse_if_recording("Trapログをエクスポート", file_path):
            return

        try:
            # ファイル拡張子で形式を判定（大文字小文字は区別しない）
            fmt = self._export_format(file_path)
            if fmt == 'csv':
                self._export_to_csv(file_path, traps)
            elif fmt == 'json':
                self._export_to_json(file_path, traps)
            else:  # .txt or other
                self._export_to_txt(file_path, traps)
            
            QMessageBox.information(self, "成功", f"Trapログをエクスポートしました:\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "エラー",
                                 self._export_error_text(e, file_path))
    
    def _export_to_csv(self, file_path: str, traps):
        """CSV形式で Trap を書き出す

        traps は呼び出し側がダイアログの前に固定した一覧。ここで self を
        読むと、ダイアログを開いている間に届いた Trap が混ざる
        """
        import csv

        # BOM 付き（utf-8-sig）。理由は _export_results_to_csv と同じ
        with atomic_text_write(file_path, newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            
            # ヘッダー
            writer.writerow(['時刻', '送信元IP', '送信元ポート', 'セキュリティ',
                             'Trap OID', 'VarBind OID', 'VarBind 値'])
            
            # データ（新しい順＝受け取った一覧の順）
            for trap in traps:
                timestamp = trap['timestamp']
                source_ip = trap['source_ip']
                source_port = trap.get('source_port', '')
                security = trap.get('security', '')
                trap_oid = trap['trap_oid']
                varbinds = trap['varbinds']
                head = [timestamp, source_ip, source_port, security, trap_oid]
                
                # VarBindsがある場合は各VarBindを1行として出力
                if varbinds:
                    for vb in varbinds:
                        writer.writerow(
                            [self._csv_safe(c)
                             for c in head + [vb['oid'], vb['value']]])
                else:
                    # VarBindsがない場合は1行だけ出力
                    writer.writerow([self._csv_safe(c) for c in head + ['', '']])
    
    def _export_to_json(self, file_path: str, traps):
        """JSON形式で Trap を書き出す（traps は CSV と同じ）"""
        with atomic_text_write(file_path, encoding='utf-8') as f:
            json.dump(traps, f, indent=2, ensure_ascii=False)

    def _export_to_txt(self, file_path: str, traps):
        """テキスト形式で Trap を書き出す（traps は CSV と同じ）"""
        resolver = get_resolver()
        
        with atomic_text_write(file_path, encoding='utf-8') as f:
            f.write("=" * 80 + "\n")
            f.write("SNMP Trap Log\n")
            f.write(f"エクスポート日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"総Trap数: {len(traps)}\n")
            f.write("=" * 80 + "\n\n")

            # データ（新しい順＝受け取った一覧の順）
            for i, trap in enumerate(traps, 1):
                timestamp = trap['timestamp']
                source_ip = trap['source_ip']
                source_port = trap.get('source_port', '')
                security = trap.get('security', '')
                trap_oid = trap['trap_oid']
                varbinds = trap['varbinds']
                
                # Trap OIDを名前に変換
                trap_name = resolver.resolve_oid(trap_oid) if trap_oid else 'N/A'
                
                f.write(f"[{i}] Trap受信\n")
                f.write(f"  時刻: {timestamp}\n")
                f.write(f"  送信元: {source_ip}:{source_port}\n")
                if security:
                    f.write(f"  セキュリティ: {security}\n")
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
            self.snmp_manager.operation_partial.connect(self._on_operation_partial)
            self.snmp_manager.operation_cancelled.connect(self._on_operation_cancelled)
            self.snmp_manager.trap_received.connect(self._on_trap_received)
            self.snmp_manager.trap_receiver_started.connect(self._on_trap_receiver_started)
            self.snmp_manager.trap_receiver_stopped.connect(self._on_trap_receiver_stopped)
            self.snmp_manager.error_occurred.connect(self._on_error_occurred)
    
    def _on_fw_allow(self):
        """手動でファイアウォール受信許可を追加（管理者昇格）。

        起動時には触らない（TFTP/FTP と同じ 3CDaemon 方式）ので、
        Windows の初回プロンプトを拒否した等で Trap が届かない環境は
        ここで直す。
        """
        if self.snmp_manager is None:
            self.trap_status_label.setText("SNMP マネージャがまだ用意されていません")
            return
        ok, msg = self.snmp_manager.fix_firewall(self.trap_port_spinbox.value())
        # 「反映待ち」等の理由を潰さず、そのまま見せる
        self.trap_status_label.setText(
            "ファイアウォール許可: %s (%s)" % ("完了" if ok else "未反映/失敗", msg))

    def _on_operation_partial(self, reason: str):
        """WALK が途中で途切れたことを受け取る（結果はこのあと届く）。

        取れた分は捨てずに表へ出すが、全部ではないと分からないまま
        使われると、機器に無いものを「無い」と読み違える。
        """
        self._partial_reason = reason

    # 利用者が止めた結果の、途中までの理由。書き出しの partial_reason にも入る
    USER_CANCEL_REASON = "利用者が中断"

    def _on_operation_cancelled(self, rows):
        """利用者が止めた GET/WALK の、そこまでに取れた行を受け取る

        途中で切れた WALK と同じく、全部ではないことを表示と書き出しに
        残す。0 行でも表を置き換える。前の結果を残すと、それにこの
        操作の「途中まで」とホストが付いて保存される。
        """
        self._show_stop_button(False)
        rows = list(rows)
        self.result_model.set_results(rows)
        self._result_host = self._request_host
        self._partial_reason = None
        self._last_partial_reason = self.USER_CANCEL_REASON
        self.status_label.setText(
            "途中まで（%s）: %d件（全部ではありません）"
            % (self.USER_CANCEL_REASON, len(rows)))

    def _on_operation_completed(self, success: bool, result):
        # 失敗の通知（モーダル）を開く前に隠す
        self._show_stop_button(False)
        if success:
            self.result_model.set_results(result)
            # 表の結果がどのホストのものかを、要求時の値で固定する
            self._result_host = self._request_host
            # 直前に「途中で切れた」と知らされていれば、そう書く。
            # 一度使ったら忘れる（次の完走に持ち越さない）
            reason = getattr(self, "_partial_reason", None)
            self._partial_reason = None
            # 書き出しに添えるため、次の完走かクリアまで持ち続ける。
            # 画面の表示だけだと、保存したファイルは完走した結果と
            # 区別が付かず、受け取った側が「機器に無い」と読み違える
            self._last_partial_reason = reason or None
            if reason:
                self.status_label.setText(
                    "途中まで: %d件（%s のため中断。全部ではありません）"
                    % (len(result), reason))
            else:
                self.status_label.setText(f"完了: {len(result)}件")
        else:
            QMessageBox.critical(self, "エラー", str(result))
            self.status_label.setText("エラー")
    
    def _on_trap_received(self, trap_data: dict):
        self._add_trap_to_tree(trap_data)
    
    def _trap_display_time(self, trap_data: dict) -> str:
        """表示・保存に使う時刻を決める（受信スレッドが付けた時刻を優先）

        Trap を受け取るのは受信スレッドで、ここが動くのは GUI スレッド。
        trap_received は queued 配送なので、その間に時間が空く。定常状態の
        ずれはミリ秒だが、GUI が滞留するとき —— 大量 Trap の配送、終了待ち、
        モーダルダイアログを開いている間 —— は意味のある差になる。now() で
        付け直すと滞留分だけ後ろへずれた時刻が表示され、そのままエクスポート
        にも入り、Trap の前後関係を時刻で追う用途で読み違える。

        received_at が無い、あるいは ISO 8601 として読めないときだけ now()
        に落とす。時刻列を空にするよりは処理時刻のほうがまだ使える。
        """
        received_at = trap_data.get('received_at')
        if received_at:
            try:
                return datetime.fromisoformat(received_at).strftime(
                    '%Y-%m-%d %H:%M:%S')
            except (TypeError, ValueError):
                pass
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def _add_trap_to_tree(self, trap_data: dict):
        """TrapデータをツリーViewに追加"""
        timestamp = self._trap_display_time(trap_data)
        source_ip = trap_data.get('source_ip', '')
        source_port = trap_data.get('source_port', 0)
        security = describe_trap_security(trap_data)
        trap_oid = trap_data.get('trap_oid', 'N/A')
        varbinds = trap_data.get('varbinds', [])
        
        # 完全なデータを保存（エクスポート用）
        self.trap_data_list.insert(0, {
            'timestamp': timestamp,
            'source_ip': source_ip,
            'source_port': source_port,
            'security': security,
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
        security_item = QStandardItem(security)
        trap_oid_item = QStandardItem(trap_name)
        varbinds_count = f"{len(filtered_vbs)} VarBinds" if filtered_vbs else "VarBindsなし"
        value_item = QStandardItem(varbinds_count)
        
        # 親行をツリーのルートに挿入（最新を先頭に）
        self.trap_tree_model.insertRow(0, [timestamp_item, source_ip_item,
                                          security_item, trap_oid_item, value_item])
        
        # 各VarBindを子アイテムとして追加
        for vb in filtered_vbs:
            oid = vb['oid']
            value = vb['value']
            
            # OIDを名前に変換
            oid_name = resolver.resolve_oid(oid)
            
            # 子アイテム作成（1行につき1つのVarBind）
            child_timestamp = QStandardItem("")  # 空
            child_source = QStandardItem("")  # 空
            child_security = QStandardItem("")  # 空
            child_oid = QStandardItem(oid_name)
            child_value = QStandardItem(value)
            
            # 親の最初の列に子行を追加
            timestamp_item.appendRow([child_timestamp, child_source,
                                      child_security, child_oid, child_value])

        # 上限を超えたぶんの古い Trap を捨てる
        self._trim_traps()

    def _on_trap_receiver_started(self):
        """Trap受信開始時の処理"""
        print("[SNMPPanel] Trap受信が正常に開始されました")
    
    def _on_trap_receiver_stopped(self):
        """Trap受信停止時の処理"""
        print("[SNMPPanel] Trap受信が停止しました")
        self._show_trap_stopped()
    
    def _on_error_occurred(self, error: str):
        QMessageBox.warning(self, "警告", error)