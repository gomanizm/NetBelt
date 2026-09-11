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
from datetime import datetime
import json


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
    
    def __init__(self, max_messages: int = 1000):
        super().__init__()
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
    
    def add_message(self, msg: SyslogMessage):
        """メッセージを追加"""
        # 最大行数を超える場合は古いものを削除
        if len(self.messages) >= self.max_messages:
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
    
    def __init__(self):
        super().__init__()
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
    
    def __init__(self, parent=None, config_manager=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self.paused = False
        self.auto_scroll = True
        self.syslog_receiver = None  # 親ウィンドウから設定される
        
        # 設定の読み込み
        self._load_config()
        
        # モデルの初期化
        self.model = SyslogTableModel(self.max_messages)
        self.proxy_model = SyslogFilterProxyModel()
        self.proxy_model.setSourceModel(self.model)
        
        self._init_ui()
    
    def _load_config(self):
        """設定の読み込み"""
        if self.config_manager:
            settings = self.config_manager.config.get("settings", {})
            syslog_config = settings.get("syslog", {})
            self.max_messages = syslog_config.get("max_messages", 1000)
            self.auto_scroll = syslog_config.get("auto_scroll", True)
        else:
            self.max_messages = 1000
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
        
        # ステータスバー
        self.status_label = QLabel("メッセージ: 0")
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
        
        # クリアボタン
        clear_action = QAction("🗑 クリア", self)
        clear_action.triggered.connect(self._clear_messages)
        toolbar.addAction(clear_action)
        
        # エクスポートボタン
        export_action = QAction("💾 エクスポート", self)
        export_action.triggered.connect(self._export_messages)
        toolbar.addAction(export_action)
        
        toolbar.addSeparator()
        
        # 自動スクロールチェックボックス
        self.auto_scroll_checkbox = QCheckBox("自動スクロール")
        self.auto_scroll_checkbox.setChecked(self.auto_scroll)
        self.auto_scroll_checkbox.stateChanged.connect(self._toggle_auto_scroll)
        toolbar.addWidget(self.auto_scroll_checkbox)
        
        return toolbar
    
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
            config = self.config_manager.config
            return config.get("settings", {}).get("syslog", {}).get("listen_port", 514)
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
    def _export_line(msg: SyslogMessage) -> str:
        """保存用の1行を作る（画面と同じく送信元を含め、機器を区別できるようにする）"""
        return f"{msg.timestamp} {msg.source_ip} {msg.hostname} [{msg.level}] {msg.message}"

    def _export_messages(self):
        """メッセージをエクスポート"""
        filename, _ = QFileDialog.getSaveFileName(
            self, "メッセージをエクスポート",
            f"syslog_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "テキストファイル (*.txt);;JSONファイル (*.json);;すべてのファイル (*.*)"
        )

        if filename:
            try:
                messages = self.model.get_all_messages()
                if filename.endswith('.json'):
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
                    with open(filename, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                else:
                    # テキスト形式でエクスポート
                    with open(filename, 'w', encoding='utf-8') as f:
                        for msg in messages:
                            f.write(self._export_line(msg) + "\n")
                
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
        copy_action = QAction("コピー", self)
        copy_action.triggered.connect(self._copy_selected)
        menu.addAction(copy_action)
        
        # 選択行をログ保存
        save_action = QAction("選択行をログ保存", self)
        save_action.triggered.connect(self._save_selected)
        menu.addAction(save_action)
        
        menu.addSeparator()
        
        # 全てクリア
        clear_action = QAction("全てクリア", self)
        clear_action.triggered.connect(self._clear_messages)
        menu.addAction(clear_action)
        
        menu.exec(self.table_view.viewport().mapToGlobal(pos))
    
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

        filename, _ = QFileDialog.getSaveFileName(
            self, "選択行を保存",
            f"syslog_selected_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "テキストファイル (*.txt);;すべてのファイル (*.*)"
        )

        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    for msg in messages:
                        f.write(self._export_line(msg) + "\n")

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
        """Syslogレシーバーを設定（親ウィンドウから呼ばれる）"""
        self.syslog_receiver = receiver
    
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