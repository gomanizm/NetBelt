"""
Syslogビューアパネル
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableView, QHeaderView,
    QPushButton, QLineEdit, QLabel, QComboBox, QCheckBox,
    QGroupBox, QGridLayout, QMenu, QFileDialog, QMessageBox, QToolBar, QSpinBox
)
from PyQt6.QtCore import Qt, QAbstractTableModel, QModelIndex, QSortFilterProxyModel, pyqtSignal, QEvent
from PyQt6.QtGui import QColor, QBrush, QAction, QStandardItemModel, QStandardItem
from PyQt6 import sip
from datetime import datetime
import json
import os
import re
import tempfile

from core import save_defaults


def _write_text_file_atomically(filename, write_body,
                                encoding='utf-8', newline=None):
    """保存先を壊さずにテキストを書き出す

    保存先を直接 open('w') すると、その時点で旧内容は失われ、書き込み中の
    失敗（満杯・共有切断・USB 取り外し）では新旧どちらでもない部分ファイルが
    残る。利用者が既存ファイルを保存先に選んで上書きを承諾した場合、
    旧内容が黙って消えることになる。
    同じディレクトリの一時ファイルへ書き切ってから os.replace で差し替え、
    書き切れなかったときは一時ファイルを消して保存先には触れない。
    （全ログ保存 ui/dialogs/log_save_dialog.py と同じ作法）

    Args:
        filename: 保存先のパス
        write_body: 開いたファイルオブジェクトを受け取って中身を書く関数
        encoding: 書き出す文字コード（CSV は Excel 向けに utf-8-sig）
        newline: open() へ渡す改行の扱い（csv.writer は newline='' を要する）
    """
    tmp_path = None
    try:
        target = os.path.abspath(filename)
        fd, tmp_path = tempfile.mkstemp(
            prefix=os.path.basename(target) + ".", suffix=".tmp",
            dir=os.path.dirname(target))
        with open(fd, 'w', encoding=encoding, newline=newline) as f:
            write_body(f)

        # 閉じてから差し替える（Windows では開いたままだと置き換えられない）
        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass   # 消せなくても保存先は無傷。残骸は .tmp なので見分けがつく


# 表計算ソフトが数式として解釈しうる値の先頭文字と、素の数の形
_CSV_FORMULA_STARTERS = "=+-@"
_CSV_PLAIN_NUMBER = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _csv_safe(value):
    """表計算ソフトが数式として解釈しうる値を、文字列として書き出す

    Syslog は認証なしの UDP で届き、本文もホスト名も送り手が決められる。
    先頭が = + - @ の値をそのまま CSV へ書くと、受け取った側が Excel /
    LibreOffice で開いた瞬間に数式（DDE を含む）として評価される。
    エクスポートは障害チケットや報告書へ回る前提なので、発火するのは
    こちらの端末とは限らない。

    判定は SNMPPanel._csv_safe と同じ（前置きの空白は落としてから見る。
    空白を 1 つ置くだけで抜けられるため）。数として読める値は、表として
    読みにくくなるのでそのまま通す。
    """
    text = "" if value is None else str(value)
    if not text:
        return text
    stripped = text.lstrip()
    if not stripped or stripped[0] not in _CSV_FORMULA_STARTERS:
        return text
    if _CSV_PLAIN_NUMBER.match(stripped):
        return text
    return "'" + text


# str.splitlines() が行の区切りとみなす文字のうち、改行・復帰（_escape_for_export
# が置き換える）以外と、その見える表記。ソースへ生の制御文字を書かないよう
# chr() で組み立てる（ui/plain_log.py と同じ作法・同じ表記）
_BACKSLASH = chr(92)
_TEXT_LINE_BREAKS = (
    (chr(0x0B), _BACKSLASH + "v"),
    (chr(0x0C), _BACKSLASH + "f"),
    (chr(0x1C), _BACKSLASH + "x1c"),
    (chr(0x1D), _BACKSLASH + "x1d"),
    (chr(0x1E), _BACKSLASH + "x1e"),
    (chr(0x85), _BACKSLASH + "u0085"),
    (chr(0x2028), _BACKSLASH + "u2028"),
    (chr(0x2029), _BACKSLASH + "u2029"),
)


class CheckableComboBox(QComboBox):
    """項目ごとにチェック ON/OFF できるドロップダウン。選択しても閉じない（複数トグル可）。"""
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().installEventFilter(self)
        self.setModel(QStandardItemModel(self))
        self.view().viewport().installEventFilter(self)

    def add_checkable(self, text, checked=True):
        item = QStandardItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        item.setData(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked,
                     Qt.ItemDataRole.CheckStateRole)
        self.model().appendRow(item)
        self._update_text()

    def checked_items(self):
        m = self.model()
        return {m.item(i).text() for i in range(m.rowCount())
                if m.item(i).checkState() == Qt.CheckState.Checked}

    def _update_text(self):
        checked = self.checked_items()
        total = self.model().rowCount()
        if total and len(checked) == total:
            t = "全レベル"
        elif not checked:
            t = "（なし）"
        else:
            t = "%d/%d 選択" % (len(checked), total)
        self.lineEdit().setText(t)

    def eventFilter(self, obj, event):
        # 自分の入力欄と一覧に掛けたフィルタなので、窓が GC で片付けられる
        # 途中、ラッパーが破棄済みになったあとも呼ばれることがある。self を
        # 引くと RuntimeError になり、excepthook が既定のテストでは PyQt が
        # qFatal でプロセスを落とした（実測）。破棄済みなら素通しにする
        if sip.isdeleted(self):
            return False
        if obj is self.lineEdit() and event.type() == QEvent.Type.MouseButtonRelease:
            if self.view().isVisible():
                self.hidePopup()
            else:
                self.showPopup()
            return True
        if obj is self.view().viewport() and event.type() == QEvent.Type.MouseButtonRelease:
            idx = self.view().indexAt(event.position().toPoint())
            item = self.model().itemFromIndex(idx)
            if item is not None:
                new = (Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked
                       else Qt.CheckState.Checked)
                item.setData(new, Qt.ItemDataRole.CheckStateRole)
                self._update_text()
                self.changed.emit()
            return True  # 消費してポップアップを閉じない
        return super().eventFilter(obj, event)

class SyslogMessage:
    """Syslogメッセージのデータクラス"""
    def __init__(self, timestamp: str, hostname: str, level: str, message: str, raw: str, source_ip: str = ""):
        self.timestamp = timestamp
        self.hostname = hostname
        self.level = level
        self.message = message
        self.raw = raw
        self.source_ip = source_ip


class SyslogTableModel(QAbstractTableModel):
    """Syslogメッセージテーブルモデル"""
    
    # レベルごとの色定義
    LEVEL_COLORS = {
        "Emergency": QColor(255, 0, 0),      # 赤
        "Alert": QColor(255, 0, 0),          # 赤
        "Critical": QColor(255, 0, 0),       # 赤
        "Error": QColor(255, 165, 0),        # オレンジ
        "Warning": QColor(255, 255, 0),      # 黄
        "Notice": QColor(255, 255, 255),     # 白
        "Info": QColor(255, 255, 255),       # 白
        "Debug": QColor(128, 128, 128),      # グレー
    }
    
    def __init__(self, max_messages: int = 1000, parent=None):
        super().__init__(parent)
        self.messages = []
        self.max_messages = max_messages
        self.headers = ["タイムスタンプ", "送信元 (プロトコル/ポート)", "レベル", "メッセージ"]
    
    def rowCount(self, parent=QModelIndex()):
        return len(self.messages)
    
    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)
    
    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        
        message = self.messages[index.row()]
        column = index.column()
        
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return message.timestamp
            elif column == 1:
                return message.source_ip
            elif column == 2:
                return message.level
            elif column == 3:
                # hostnameとmessageを組み合わせて表示
                if message.hostname:
                    return f"{message.hostname}: {message.message}"
                return message.message
        
        elif role == Qt.ItemDataRole.BackgroundRole:
            # レベルに応じた背景色
            level = message.level
            if level in self.LEVEL_COLORS:
                return QBrush(self.LEVEL_COLORS[level])
        
        elif role == Qt.ItemDataRole.ForegroundRole:
            # 文字色（背景に応じて調整）
            level = message.level
            if level in ["Warning", "Notice", "Info"]:
                return QBrush(QColor(0, 0, 0))  # 黒文字
            else:
                return QBrush(QColor(255, 255, 255))  # 白文字
        
        return None
    
    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
        return None
    
    def _limit_reached(self):
        """保持件数が上限に達しているか。上限が数でなければ達していない扱い。

        max_messages は手編集の config.json から来ることがあり、null や
        文字列だと比較そのものが TypeError になる。ここで受け止めないと、
        受信スロットを例外が抜けて PyQt6 がプロセスを落とす
        """
        try:
            return len(self.messages) >= int(self.max_messages)
        except (TypeError, ValueError):
            return False

    def add_message(self, msg: SyslogMessage):
        """メッセージを追加"""
        # 最大行数を超える場合は古いものを削除。保持している行があるときだけ
        # 削除する。上限が 0 以下だと条件が常に真になり、beginRemoveRows の
        # 後の pop(0) が空リストで失敗して、行削除通知が開いたまま残っていた
        if self.messages and self._limit_reached():
            self.beginRemoveRows(QModelIndex(), 0, 0)
            self.messages.pop(0)
            self.endRemoveRows()

        # 新しいメッセージを追加
        row = len(self.messages)
        self.beginInsertRows(QModelIndex(), row, row)
        self.messages.append(msg)
        self.endInsertRows()
    
    def clear_messages(self):
        """全メッセージをクリア"""
        self.beginResetModel()
        self.messages.clear()
        self.endResetModel()
    
    def get_message(self, row: int) -> SyslogMessage:
        """指定行のメッセージを取得"""
        if 0 <= row < len(self.messages):
            return self.messages[row]
        return None
    
    def get_all_messages(self):
        """全メッセージを取得"""
        return self.messages.copy()


class SyslogFilterProxyModel(QSortFilterProxyModel):
    """Syslogメッセージフィルタプロキシモデル"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.enabled_levels = set(SyslogTableModel.LEVEL_COLORS.keys())
        self.hostname_filter = ""
        self.keyword_filter = ""
    
    def set_level_filter(self, levels: set):
        """レベルフィルタを設定"""
        self.enabled_levels = levels
        self.invalidateFilter()
    
    def set_hostname_filter(self, hostname: str):
        """ホストフィルタを設定"""
        self.hostname_filter = hostname
        self.invalidateFilter()
    
    def set_keyword_filter(self, keyword: str):
        """キーワードフィルタを設定"""
        self.keyword_filter = keyword
        self.invalidateFilter()
    
    def filterAcceptsRow(self, source_row, source_parent):
        """フィルタ条件に基づいて行を受け入れるかどうかを判定"""
        model = self.sourceModel()
        
        # レベルフィルタ
        level_index = model.index(source_row, 2, source_parent)
        level = model.data(level_index, Qt.ItemDataRole.DisplayRole)
        if level not in self.enabled_levels:
            return False
        
        # ホストフィルタは削除されたので無効化
        # if self.hostname_filter:
        #     host_index = model.index(source_row, 2, source_parent)
        #     host = model.data(host_index, Qt.ItemDataRole.DisplayRole)
        #     if self.hostname_filter != host:
        #         return False
        
        # キーワードフィルタ
        if self.keyword_filter:
            msg_index = model.index(source_row, 3, source_parent)
            message = model.data(msg_index, Qt.ItemDataRole.DisplayRole)
            if self.keyword_filter.lower() not in message.lower():
                return False
        
        return True


class SyslogPanel(QWidget):
    """Syslogビューアパネル"""

    # 保持するメッセージ件数の既定値
    DEFAULT_MAX_MESSAGES = 1000

    # アプリの終了処理に入ったか（MainWindow.closeEvent が立てる）。
    # 立っている間はモーダルを開かない。FTP / TFTP / SFTP の各パネルと同じ理由
    # （終了処理は配送待ちのシグナルをその場で配るので、受信スレッドが出した
    # エラーもそこで届く。答えるまで終了が止まり、そのとき受信は停止済み）。
    _closing = False

    def __init__(self, parent=None, config_manager=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self.paused = False
        self.auto_scroll = True
        self.syslog_receiver = None  # 親ウィンドウから設定される
        
        # 設定の読み込み
        self._load_config()
        
        # モデルの初期化
        # 親を持たせる（setModel / setSourceModel は所有権を取らない）
        self.model = SyslogTableModel(self.max_messages, self)
        self.proxy_model = SyslogFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.model)
        
        self._init_ui()
    
    def _syslog_settings(self):
        """settings.syslog を dict で返す。

        config.json は手で編集できるので、settings や syslog が null などの
        dict でない値になっていることがある。そのまま .get を呼ぶと
        AttributeError でパネル（ひいては MainWindow）の構築が失敗するので、
        dict でなければ既定値（空の設定）として扱う。
        """
        settings = self.config_manager.config.get("settings", {})
        section = settings.get("syslog", {}) if isinstance(settings, dict) else {}
        return section if isinstance(section, dict) else {}

    @staticmethod
    def _sanitize_max_messages(value):
        """保持件数を int へ寄せる。使えない値は既定値へ戻す。

        config.json は手で編集できるうえ、max_messages は GUI からもアプリからも
        書かれないので、ここへ来る値は手書きのものだけ。null・0・負数・文字列が
        入ると最初の受信で add_message が例外になり、それが受信スロットを抜けて
        PyQt6 がプロセスを落としていた（メッセージも出ずに終了）。auto_scroll と
        同じく、値が不正でも起動と受信は続けたいので既定値へ戻す
        """
        if isinstance(value, bool):
            return SyslogPanel.DEFAULT_MAX_MESSAGES
        try:
            limit = int(value)
        except (TypeError, ValueError):
            return SyslogPanel.DEFAULT_MAX_MESSAGES
        return limit if limit >= 1 else SyslogPanel.DEFAULT_MAX_MESSAGES

    def _load_config(self):
        """設定の読み込み"""
        if self.config_manager:
            syslog_config = self._syslog_settings()
            self.max_messages = self._sanitize_max_messages(
                syslog_config.get("max_messages", self.DEFAULT_MAX_MESSAGES))
            self.auto_scroll = syslog_config.get("auto_scroll", True)
        else:
            self.max_messages = self.DEFAULT_MAX_MESSAGES
            self.auto_scroll = True
    
    def _init_ui(self):
        """UIの初期化"""
        layout = QVBoxLayout()
        layout.setContentsMargins(5, 5, 5, 5)
        
        # ツールバー
        toolbar = self._create_toolbar()
        layout.addWidget(toolbar)

        # 受信の開始/停止（TFTP/FTP/SFTP サーバーと同じ見た目・配置に統一）
        recv_btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("▶ 受信開始")
        self.start_btn.clicked.connect(self._toggle_receiver)
        self.start_btn.setStyleSheet("QPushButton { background-color: #4CAF50; color: white; padding: 8px; font-weight: bold; }")
        recv_btn_layout.addWidget(self.start_btn)
        self.stop_btn = QPushButton("⬛ 受信停止")
        self.stop_btn.clicked.connect(self._toggle_receiver)
        self.stop_btn.setVisible(False)
        self.stop_btn.setStyleSheet("QPushButton { background-color: #f44336; color: white; padding: 8px; font-weight: bold; }")
        recv_btn_layout.addWidget(self.stop_btn)
        layout.addLayout(recv_btn_layout)

        # 手動FW許可（3CDaemon方式で通らない時の復旧用・押した時だけ管理者昇格/UAC）
        fw_layout = QHBoxLayout()
        self.fw_allow_btn = QPushButton("ファイアウォールで許可（管理者）")
        self.fw_allow_btn.setToolTip("受信できない場合に押してください。Windowsファイアウォールの受信許可を追加します（管理者昇格/UACが1回出ます）。")
        self.fw_allow_btn.clicked.connect(self._on_fw_allow)
        fw_layout.addWidget(self.fw_allow_btn)
        fw_layout.addStretch()
        layout.addLayout(fw_layout)

        # 受信状態（サーバーパネルと同じ GroupBox 形式）
        recv_status_group = QGroupBox("受信状態")
        recv_status_layout = QVBoxLayout()
        self.recv_status_label = QLabel("🔴 停止中")
        self.recv_status_label.setStyleSheet("color: #f44336; font-weight: bold; font-size: 14px;")
        recv_status_layout.addWidget(self.recv_status_label)
        recv_status_group.setLayout(recv_status_layout)
        layout.addWidget(recv_status_group)
        
        # フィルタエリア
        filter_group = self._create_filter_area()
        layout.addWidget(filter_group)

        # 表示操作ボタン（一覧のすぐ上に左寄せ1行。SNMP の Trap 受信と同じ形）
        layout.addLayout(self._create_list_buttons())

        # テーブルビュー
        self.table_view = QTableView()
        self.table_view.setModel(self.proxy_model)
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self._show_context_menu)
        
        # カラムの幅設定
        header = self.table_view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)  # 全列を手動調整可能に
        # 初期幅を設定
        header.resizeSection(0, 150)  # タイムスタンプ
        header.resizeSection(1, 190)  # 送信元 (プロトコル/ポート)
        header.resizeSection(2, 80)   # レベル
        # メッセージ列は残りのスペースを使用（最後の列なので自動的に伸びる）
        header.setStretchLastSection(True)  # 最後の列を伸縮可能に
        
        layout.addWidget(self.table_view)
        
        # ステータスバー。ファイアウォール許可の結果など長い文面が入るので
        # 折り返す。折り返さないと、その文面が出た瞬間にパネルの最小幅が
        # 伸び、窓を縮められなくなる
        self.status_label = QLabel("メッセージ: 0")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        
        self.setLayout(layout)
    
    def _create_toolbar(self):
        """ツールバーの作成"""
        toolbar = QToolBar()
        
        # プロトコルごとに有効/無効とポートを指定（受信中でも増減が即反映される）
        self.udp_check = QCheckBox("UDP")
        self.udp_check.setChecked(True)
        self.udp_check.toggled.connect(lambda on: self._on_protocol_toggled("UDP", on))
        toolbar.addWidget(self.udp_check)
        self.udp_port_spin = QSpinBox()
        self.udp_port_spin.setRange(1, 65535)
        self.udp_port_spin.setValue(514)
        self.udp_port_spin.setToolTip("UDP の待受ポート")
        toolbar.addWidget(self.udp_port_spin)
        toolbar.addSeparator()
        self.tcp_check = QCheckBox("TCP")
        self.tcp_check.setChecked(False)
        self.tcp_check.toggled.connect(lambda on: self._on_protocol_toggled("TCP", on))
        toolbar.addWidget(self.tcp_check)
        self.tcp_port_spin = QSpinBox()
        self.tcp_port_spin.setRange(1, 65535)
        self.tcp_port_spin.setValue(514)
        self.tcp_port_spin.setToolTip("TCP の待受ポート（UDP と別番号でもよい）")
        toolbar.addWidget(self.tcp_port_spin)
        
        
        toolbar.addSeparator()
        
        # 一時停止/再開ボタン
        self.pause_action = QAction("⏸ 一時停止", self)
        self.pause_action.triggered.connect(self._toggle_pause)
        self.pause_action.setEnabled(False)  # 初期状態では無効
        toolbar.addAction(self.pause_action)
        
        toolbar.addSeparator()

        # クリアとエクスポートはツールバーに置かない。他の画面と同じく
        # 一覧のすぐ上のボタン行（_create_list_buttons）へ移した。
        # ここに残すのは受信まわり（プロトコルとポート・一時停止）だけ

        # 自動スクロールチェックボックス
        self.auto_scroll_checkbox = QCheckBox("自動スクロール")
        self.auto_scroll_checkbox.setChecked(self.auto_scroll)
        self.auto_scroll_checkbox.stateChanged.connect(self._toggle_auto_scroll)
        toolbar.addWidget(self.auto_scroll_checkbox)
        
        return toolbar
    
    def _create_list_buttons(self):
        """一覧のすぐ上に置く、左寄せ1行のボタン行を作る

        押したときの動きはツールバーにあったときと同じ（クリアは確認を
        出し、エクスポートは記録中のファイルを断る）。
        """
        row = QHBoxLayout()
        self.export_button = QPushButton("エクスポート")
        self.export_button.clicked.connect(self._export_messages)
        row.addWidget(self.export_button)
        self.clear_button = QPushButton("クリア")
        self.clear_button.clicked.connect(self._clear_messages)
        row.addWidget(self.clear_button)
        # 余った幅は行末の空きへ（無いとボタン自身が横へ間延びする）
        row.addStretch()
        return row

    def _create_filter_area(self):
        """フィルタエリアの作成"""
        group = QGroupBox("フィルタ")
        layout = QGridLayout()
        
        # レベルフィルタ（チェック可能なドロップダウン。横並びチェックボックスの横長を回避）
        layout.addWidget(QLabel("レベル:"), 0, 0)
        self.level_combo = CheckableComboBox()
        self.level_combo.setMinimumWidth(140)
        self.level_combo.setMaximumWidth(220)
        for level in SyslogTableModel.LEVEL_COLORS.keys():
            self.level_combo.add_checkable(level, True)
        self.level_combo.changed.connect(self._apply_filters)
        layout.addWidget(self.level_combo, 0, 1)
        
        # キーワード検索
        layout.addWidget(QLabel("キーワード:"), 1, 0)
        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("検索キーワードを入力...")
        self.keyword_edit.textChanged.connect(self._apply_filters)
        layout.addWidget(self.keyword_edit, 1, 1)
        
        group.setLayout(layout)
        return group
    
    def _on_fw_allow(self):
        """手動でファイアウォール受信許可を追加（管理者昇格）。

        起動時には触らない（TFTP/FTP と同じ 3CDaemon 方式）ので、
        Windows の初回プロンプトを拒否した等で受信できない環境はここで直す。
        """
        if self.syslog_receiver is None:
            # 受信器は親ウィンドウから後で入る。押せる状態でも未設定はあり得る
            self.status_label.setText("受信器がまだ用意されていません")
            return
        self.status_label.setText("ファイアウォール許可を実行します（管理者昇格）...")
        ok, msg = self.syslog_receiver.fix_firewall()
        # 「反映待ち」等の理由を潰さず、そのまま見せる
        self.status_label.setText(
            "ファイアウォール許可: %s (%s)" % ("完了" if ok else "未反映/失敗", msg))

    def _protocol_port(self, proto):
        """指定プロトコルの待受ポート"""
        return self.udp_port_spin.value() if proto == "UDP" else self.tcp_port_spin.value()

    def _checked_protocols(self):
        """チェックされているプロトコルを ["UDP","TCP"] の順で返す"""
        protos = []
        if self.udp_check.isChecked():
            protos.append("UDP")
        if self.tcp_check.isChecked():
            protos.append("TCP")
        return protos

    def _set_ports_enabled(self, enabled):
        """稼働中はポート変更を禁止する（プロトコルの増減は可）"""
        self.udp_port_spin.setEnabled(enabled)
        self.tcp_port_spin.setEnabled(enabled)

    def _toggle_receiver(self):
        """受信の開始/停止（チェックされたプロトコルをまとめて扱う）"""
        if self.syslog_receiver is None:
            QMessageBox.warning(self, "エラー", "Syslogレシーバーが設定されていません。")
            return

        if self.syslog_receiver.is_running:
            self._stop_all()
            return

        protos = self._checked_protocols()
        if not protos:
            QMessageBox.warning(self, "エラー", "UDP か TCP のどちらかを選択してください。")
            return
        started = []
        for proto in protos:
            if self.syslog_receiver.start_protocol(proto, self._protocol_port(proto)):
                started.append("%s/%d" % (proto, self._protocol_port(proto)))
        if not started:
            self._update_recv_status()
            return
        self.start_btn.setVisible(False)
        self.stop_btn.setVisible(True)
        self.pause_action.setEnabled(True)
        self._set_ports_enabled(False)
        self._update_recv_status()
        QMessageBox.information(self, "Syslog受信", "%s で受信を開始しました。" % " / ".join(started))

    def _stop_all(self):
        """全プロトコルを停止し、UI を停止状態へ戻す（モーダルは出さない）"""
        if self.syslog_receiver is not None:
            self.syslog_receiver.stop()
        self.start_btn.setVisible(True)
        self.stop_btn.setVisible(False)
        self.pause_action.setEnabled(False)
        self._set_ports_enabled(True)
        self._update_recv_status()

    def _on_protocol_toggled(self, proto, checked):
        """受信中にチェックが変わったら、そのプロトコルだけ無停止で起動/停止する。

        注意: このハンドラから _toggle_receiver() を呼ぶと、チェック操作 -> 停止処理 ->
        再びチェック更新…と再入して固まる。ここでは起動/停止と表示更新だけを行う。
        """
        recv = self.syslog_receiver
        if recv is None or not recv.is_running:
            return   # 停止中は次回の受信開始時に反映される
        if checked:
            if not recv.start_protocol(proto, self._protocol_port(proto)):
                # 起動失敗（ポート使用中など）はチェックを戻す（シグナル再入を防ぐ）
                box = self.udp_check if proto == "UDP" else self.tcp_check
                box.blockSignals(True)
                box.setChecked(False)
                box.blockSignals(False)
        else:
            recv.stop_protocol(proto)
            if not recv.is_running:
                # 最後の1つを外した = 全停止。UI も停止状態へ戻す
                self._stop_all()
                return
        self._update_recv_status()

    def _update_recv_status(self):
        """受信状態ラベルを稼働中プロトコルと実ポートで更新する"""
        recv = self.syslog_receiver
        actives = recv.active_protocols() if recv is not None else []
        if actives:
            parts = ["%s %s" % (pr, recv.active_port(pr)) for pr in actives]
            self.recv_status_label.setText("🔵 受信中 (%s)" % " / ".join(parts))
            self.recv_status_label.setStyleSheet("color: #2196F3; font-weight: bold; font-size: 14px;")
        else:
            self.recv_status_label.setText("🔴 停止中")
            self.recv_status_label.setStyleSheet("color: #f44336; font-weight: bold; font-size: 14px;")

    def _get_listen_port(self):
        """設定から待ち受けポートを取得"""
        if self.config_manager:
            return self._syslog_settings().get("listen_port", 514)
        return 514
    
    def _toggle_pause(self):
        """一時停止/再開の切り替え"""
        self.paused = not self.paused
        if self.paused:
            self.pause_action.setText("▶ 再開")
        else:
            self.pause_action.setText("⏸ 一時停止")
    
    def _clear_messages(self):
        """全メッセージをクリア"""
        reply = QMessageBox.question(
            self, "確認",
            "すべてのメッセージをクリアしますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.model.clear_messages()
            self._update_status()
    
    @staticmethod
    def _escape_for_export(text: str) -> str:
        """保存・コピー時に1件が1行へ収まるよう、改行・復帰・タブを表記へ置き換える

        本文やホスト名の改行をそのまま書くと、続きの行が別機器の独立した記録に
        見える。認証なしで届く 1 件の Syslog に「別日時 別IP 別ホスト
        [Emergency] 偽の記録」を仕込めば、保存した証跡へ任意の記録を混ぜられる
        （画面のテーブルでは 1 行のままなので突き合わせても気づけない）。
        バックスラッシュ自身も置き換えないと、元から \\n と書かれていた本文と
        改行由来の表記を区別できない。
        """
        return (str(text)
                .replace("\\", "\\\\")
                .replace("\r", "\\r")
                .replace("\n", "\\n")
                .replace("\t", "\\t"))

    @classmethod
    def _escape_for_text(cls, text: str) -> str:
        """テキストの 1 行へ入れる値を作る（テキスト保存・選択行の保存・コピー）

        _escape_for_export に加えて、str.splitlines() が行の区切りとみなす残りの
        文字（U+000B / U+000C / U+001C〜U+001E / U+0085 / U+2028 / U+2029）も
        見える表記へ置き換える。残すと、Unicode の改行を解するビューアや
        splitlines() では 1 件が割れ、続きが別機器の独立した記録に見える。
        バックスラッシュは先に二重にしてあるので、元の綴りとは区別できる。
        CSV は値を引用符で囲む形式なので使わない（中身を変えない）
        """
        text = cls._escape_for_export(text)
        for char, shown in _TEXT_LINE_BREAKS:
            if char in text:
                text = text.replace(char, shown)
        return text

    @classmethod
    def _export_line(cls, msg: SyslogMessage) -> str:
        """保存用の1行を作る（画面と同じく送信元を含め、機器を区別できるようにする）"""
        hostname = cls._escape_for_text(msg.hostname)
        message = cls._escape_for_text(msg.message)
        return f"{msg.timestamp} {msg.source_ip} {hostname} [{msg.level}] {message}"

    def _refuse_if_recording(self, title: str, file_path: str) -> bool:
        """保存先が端末のログ記録に使われていたら断る（断ったら True）

        保存は保存先を別の内容へ作り直す（_write_text_file_atomically が
        一時ファイルへ書き切ってから os.replace で置き換える）。記録中の
        ファイルを選ばれると、置き換えが通れば記録済みの内容は失われ、端末は
        開いたままのハンドルで自分のオフセットから書き続けるので、双方の
        ファイルが壊れる。置き換えが Windows の共有違反で弾かれた場合も、
        利用者に出るのは汎用の保存失敗になり理由が分からない。
        （SNMPPanel._refuse_if_recording と同じ判定）
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
    def _export_format(file_path: str) -> str:
        """保存先の拡張子から書き出す形式を決める（"csv" / "json" / "txt"）

        大文字小文字は区別しない。区別すると out.JSON がテキストの中身で
        書かれたうえ「エクスポートしました」と成功扱いになり、中身と拡張子
        の食い違ったファイルが残る（SNMPPanel._export_format と同じ理由）。
        ダイアログは選んだフィルタの拡張子を小文字で補うので普段は当たるが、
        利用者が自分で .JSON と打った場合と、大文字名の既存ファイルを選び
        直した場合に外れる。

        当てはまらない拡張子は従来どおり TXT（既定の形式）。
        """
        ext = os.path.splitext(file_path)[1].lower()
        if ext in ('.csv', '.json'):
            return ext[1:]
        return 'txt'

    def _export_messages_to_csv(self, filename, messages):
        """CSV で書き出す（1 件 1 行。列はテキスト形式と同じ並び）

        BOM 付き（utf-8-sig）。日本語版 Excel は BOM の無い UTF-8 の CSV を
        cp932 として開くため、見出しも機器から来た日本語も文字化けする
        （SNMPPanel._export_results_to_csv と同じ）。

        本文とホスト名はテキスト形式と同じく改行・タブを表記へ置き換える。
        CSV は引用符で囲めば改行を持てるが、1 件が複数行になると行単位の
        突き合わせで別機器の独立した記録と見分けが付かなくなる。
        """
        import csv

        def write_rows(f):
            writer = csv.writer(f)
            writer.writerow(['時刻', '送信元', 'ホスト名', 'レベル', 'メッセージ'])
            for msg in messages:
                writer.writerow([_csv_safe(value) for value in (
                    msg.timestamp,
                    msg.source_ip,
                    self._escape_for_export(msg.hostname),
                    msg.level,
                    self._escape_for_export(msg.message),
                )])

        _write_text_file_atomically(filename, write_rows,
                                    encoding='utf-8-sig', newline='')

    def _export_messages(self):
        """メッセージをエクスポート（txt/csv/json）"""
        default_name = f"syslog_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        filename, selected_filter = QFileDialog.getSaveFileName(
            self, "メッセージをエクスポート",
            save_defaults.initial_path(self.config_manager, default_name),
            save_defaults.TABLE_FILTERS
        )

        if filename:
            # 選んだ種類に合わせて拡張子を付け替える（中身は拡張子で決まるので、
            # ここで揃えないと CSV を選んでもテキストが書かれる）。付け替えた
            # 先が既にあれば、ダイアログが訊いていない上書きなので確認する
            filename = save_defaults.apply_filter_suffix_confirmed(
                self, "メッセージをエクスポート", filename, selected_filter,
                default_name)
            if not filename:
                return
            if self._refuse_if_recording("メッセージをエクスポート", filename):
                return
            try:
                messages = self.model.get_all_messages()
                fmt = self._export_format(filename)
                if fmt == 'json':
                    # JSON形式でエクスポート（送信元と受信生データも残す）
                    data = [
                        {
                            "timestamp": msg.timestamp,
                            "source": msg.source_ip,
                            "hostname": msg.hostname,
                            "level": msg.level,
                            "message": msg.message,
                            "raw": msg.raw
                        }
                        for msg in messages
                    ]
                    _write_text_file_atomically(
                        filename,
                        lambda f: json.dump(data, f, ensure_ascii=False, indent=2))
                elif fmt == 'csv':
                    self._export_messages_to_csv(filename, messages)
                else:
                    # テキスト形式でエクスポート
                    def write_lines(f):
                        for msg in messages:
                            f.write(self._export_line(msg) + "\n")

                    _write_text_file_atomically(filename, write_lines)

                # 書き終えてから覚える（取り消し・失敗では変えない）
                save_defaults.remember(self.config_manager, filename)
                QMessageBox.information(self, "成功", f"メッセージを {filename} にエクスポートしました。")
            except Exception as e:
                QMessageBox.critical(self, "エラー", f"エクスポートに失敗しました: {e}")
    
    def _toggle_auto_scroll(self, state):
        """自動スクロールの切り替え"""
        self.auto_scroll = (state == Qt.CheckState.Checked.value)
    
    def _apply_filters(self):
        """フィルタを適用"""
        # レベルフィルタ
        self.proxy_model.set_level_filter(self.level_combo.checked_items())
        
        # キーワードフィルタ
        keyword = self.keyword_edit.text()
        self.proxy_model.set_keyword_filter(keyword)
        
        self._update_status()
    
    def _show_context_menu(self, pos):
        """コンテキストメニューの表示"""
        menu = QMenu(self)
        
        # コピー
        copy_action = QAction("コピー", menu)
        copy_action.triggered.connect(self._copy_selected)
        menu.addAction(copy_action)

        # 選択行をログ保存
        save_action = QAction("選択行をログ保存", menu)
        save_action.triggered.connect(self._save_selected)
        menu.addAction(save_action)

        menu.addSeparator()

        # 全てクリア
        clear_action = QAction("全てクリア", menu)
        clear_action.triggered.connect(self._clear_messages)
        menu.addAction(clear_action)

        try:
            menu.exec(self.table_view.viewport().mapToGlobal(pos))
        finally:
            # このパネルを親にしたメニューは、閉じただけでは子として残り、
            # 右クリックのたびに QMenu 1 件と項目が積み上がる。項目の親も
            # メニューにしてあるので、メニューが消えるときに一緒に片付く。
            menu.deleteLater()
    
    def _copy_selected(self):
        """選択行をコピー"""
        from PyQt6.QtWidgets import QApplication
        
        selected_rows = self.table_view.selectionModel().selectedRows()
        if not selected_rows:
            return
        
        lines = []
        for index in selected_rows:
            row = index.row()
            source_row = self.proxy_model.mapToSource(index).row()
            msg = self.model.get_message(source_row)
            if msg:
                lines.append(self._export_line(msg))
        
        QApplication.clipboard().setText("\n".join(lines))
    
    def _save_selected(self):
        """選択行をログ保存"""
        selected_rows = self.table_view.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.warning(self, "警告", "保存する行を選択してください。")
            return

        # ダイアログを開いている間に受信で先頭行が押し出されると行番号がずれるので、
        # 保存対象のメッセージはダイアログを出す前に確定しておく
        messages = []
        for index in selected_rows:
            source_row = self.proxy_model.mapToSource(index).row()
            msg = self.model.get_message(source_row)
            if msg:
                messages.append(msg)

        default_name = f"syslog_selected_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        filename, selected_filter = QFileDialog.getSaveFileName(
            self, "選択行を保存",
            save_defaults.initial_path(self.config_manager, default_name),
            "テキスト (*.txt);;すべてのファイル (*.*)"
        )

        if filename:
            # エクスポートと同じく、選んだ種類へ拡張子を合わせる
            filename = save_defaults.apply_filter_suffix_confirmed(
                self, "選択行を保存", filename, selected_filter, default_name)
            if not filename:
                return
            if self._refuse_if_recording("選択行を保存", filename):
                return
            try:
                def write_lines(f):
                    for msg in messages:
                        f.write(self._export_line(msg) + "\n")

                _write_text_file_atomically(filename, write_lines)

                # 書き終えてから覚える（取り消し・失敗では変えない）
                save_defaults.remember(self.config_manager, filename)
                QMessageBox.information(self, "成功", f"選択行を {filename} に保存しました。")
            except Exception as e:
                QMessageBox.critical(self, "エラー", f"保存に失敗しました: {e}")
    
    def _update_status(self):
        """ステータスラベルを更新"""
        total = self.model.rowCount()
        filtered = self.proxy_model.rowCount()
        if total == filtered:
            self.status_label.setText(f"メッセージ: {total}")
        else:
            self.status_label.setText(f"メッセージ: {filtered} / {total}")
    
    def set_syslog_receiver(self, receiver):
        """Syslogレシーバーを設定（親ウィンドウから呼ばれる）

        error_occurred をここで繋ぐ。繋がないと bind 失敗の理由がどこへも
        出ず、利用者からは「開始を押したが何も起こらず停止中のまま」に
        見える（FTP / TFTP / SFTP の各パネルは同じ信号を _on_error へ
        繋いでいる）。
        """
        old = getattr(self, "syslog_receiver", None)
        if old is not None and old is not receiver:
            try:
                old.error_occurred.disconnect(self._on_receiver_error)
            except (TypeError, RuntimeError):
                pass   # 繋いでいない / 既に破棄済み
        self.syslog_receiver = receiver
        if receiver is not None and receiver is not old:
            receiver.error_occurred.connect(self._on_receiver_error)

    def _on_receiver_error(self, error_message):
        """受信側の障害（ポート使用中・特権ポートなど）を利用者へ見せる

        ここへ来るのは待ち受けそのものが始められなかった場合だけ。
        受信したメッセージの不備は一覧に出るので、モーダルにはしない。
        """
        if self._closing:
            # 終了処理の途中。開いても何もできない（_closing の説明を参照）
            print("[Syslog] %s" % error_message)
            return
        QMessageBox.critical(self, "Syslog受信 エラー", error_message)


    def add_message(self, syslog_msg):
        """メッセージを追加（外部から呼び出される）"""
        if self.paused:
            return
        
        # タイムスタンプをフォーマット
        if isinstance(syslog_msg.timestamp, str):
            timestamp_str = syslog_msg.timestamp
        else:
            timestamp_str = syslog_msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        
        # SyslogMessageオブジェクトに変換
        msg = SyslogMessage(
            timestamp=timestamp_str,
            hostname=syslog_msg.hostname,
            level=syslog_msg.level,
            message=syslog_msg.message,
            raw=syslog_msg.raw_message,
            # どのプロトコル/ポートで受信したかを併記（例: 192.0.2.1 (UDP/514)）
            source_ip=getattr(syslog_msg, "source_display", None) or syslog_msg.source_ip
        )
        
        # モデルに追加
        self.model.add_message(msg)
        
        # 自動スクロール
        if self.auto_scroll:
            self.table_view.scrollToBottom()
        
        # ステータス更新
        self._update_status()