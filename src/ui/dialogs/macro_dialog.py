"""
マクロ設定ダイアログ
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox,
    QPushButton, QLabel, QSpinBox, QTextEdit,
    QListWidget, QMessageBox, QInputDialog, QLineEdit,
    QComboBox, QTabWidget, QWidget
)
from PyQt6.QtCore import Qt, pyqtSignal
from typing import List, Optional


class MacroDialog(QDialog):
    """マクロ設定ダイアログ"""
    
    # シグナル
    keepalive_start_requested = pyqtSignal(int)  # interval_seconds
    keepalive_stop_requested = pyqtSignal()
    command_list_start_requested = pyqtSignal(list, int)  # commands, delay_ms
    command_list_stop_requested = pyqtSignal()
    
    def __init__(self, parent=None, device_name: str = "", 
                 keepalive_active: bool = False,
                 command_list_active: bool = False,
                 config_manager=None,
                 keepalive_interval: int = 60):
        super().__init__(parent)
        self.device_name = device_name
        self.keepalive_active = keepalive_active
        # いま動いている（または前回使った）送信間隔。_create_ui より前に
        # 退避しておかないと、送信間隔の欄が既定値のままになり、
        # 動作中の表示も次の開始も実間隔とずれる
        self.keepalive_interval = keepalive_interval
        self.command_list_active = command_list_active
        self.config_manager = config_manager
        
        self.setWindowTitle(f"マクロ設定 - {device_name}")
        self.setMinimumWidth(600)
        self.setMinimumHeight(500)
        
        self._create_ui()
        self._load_presets()
    
    def _create_ui(self):
        """UI作成"""
        layout = QVBoxLayout(self)
        
        # タブウィジェット
        tab_widget = QTabWidget()
        
        # キープアライブタブ
        keepalive_tab = self._create_keepalive_tab()
        tab_widget.addTab(keepalive_tab, "キープアライブ")
        
        # プリセット管理タブ
        preset_tab = self._create_preset_tab()
        tab_widget.addTab(preset_tab, "プリセット管理")
        
        layout.addWidget(tab_widget)
        
        # 閉じるボタン
        close_btn = QPushButton("閉じる")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)
    
    def _create_keepalive_tab(self) -> QWidget:
        """キープアライブタブを作成"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        # 説明
        desc_label = QLabel(
            "SSHセッションを維持するために、定期的にエンターを送信します。\n"
            "タイムアウトを防ぐのに便利です。"
        )
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)
        
        # 間隔設定
        interval_layout = QHBoxLayout()
        interval_layout.addWidget(QLabel("送信間隔:"))
        
        self.keepalive_interval_spin = QSpinBox()
        self.keepalive_interval_spin.setMinimum(10)
        self.keepalive_interval_spin.setMaximum(3600)
        self.keepalive_interval_spin.setValue(self.keepalive_interval)
        self.keepalive_interval_spin.setSuffix(" 秒")
        interval_layout.addWidget(self.keepalive_interval_spin)
        interval_layout.addStretch()
        
        layout.addLayout(interval_layout)
        
        # 開始/停止ボタン
        btn_layout = QHBoxLayout()
        
        self.keepalive_start_btn = QPushButton("開始")
        self.keepalive_start_btn.clicked.connect(self._on_keepalive_start)
        btn_layout.addWidget(self.keepalive_start_btn)
        
        self.keepalive_stop_btn = QPushButton("停止")
        self.keepalive_stop_btn.clicked.connect(self._on_keepalive_stop)
        self.keepalive_stop_btn.setEnabled(False)
        btn_layout.addWidget(self.keepalive_stop_btn)
        
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        # ステータスラベル
        self.keepalive_status_label = QLabel("状態: 停止中")
        layout.addWidget(self.keepalive_status_label)
        
        layout.addStretch()
        
        # 初期状態を反映
        if self.keepalive_active:
            self._update_keepalive_ui(True)
        
        return widget
    
    def _create_preset_tab(self) -> QWidget:
        """プリセット管理タブを作成"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        # 説明
        desc_label = QLabel(
            "コマンドリストをプリセットとして保存・管理できます。\n"
            "よく使うコマンドセットを登録しておくと便利です。"
        )
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)
        
        # プリセット一覧
        list_layout = QHBoxLayout()
        
        list_v_layout = QVBoxLayout()
        list_v_layout.addWidget(QLabel("登録済みプリセット:"))
        
        self.preset_list_widget = QListWidget()
        self.preset_list_widget.itemDoubleClicked.connect(self._on_preset_edit)
        list_v_layout.addWidget(self.preset_list_widget)
        
        list_layout.addLayout(list_v_layout)
        
        # 管理ボタン
        btn_v_layout = QVBoxLayout()
        
        new_preset_btn = QPushButton("新規作成")
        new_preset_btn.clicked.connect(self._on_preset_new)
        btn_v_layout.addWidget(new_preset_btn)
        
        edit_preset_btn = QPushButton("編集")
        edit_preset_btn.clicked.connect(self._on_preset_edit)
        btn_v_layout.addWidget(edit_preset_btn)
        
        delete_preset_btn = QPushButton("削除")
        delete_preset_btn.clicked.connect(self._on_preset_delete)
        btn_v_layout.addWidget(delete_preset_btn)
        
        btn_v_layout.addStretch()
        
        list_layout.addLayout(btn_v_layout)
        
        layout.addLayout(list_layout)
        
        return widget
    
    def _load_presets(self):
        """プリセット一覧を読み込み"""
        if not self.config_manager:
            return
        
        # リストウィジェットをクリア
        self.preset_list_widget.clear()
        
        # プリセットを読み込み
        macros = self.config_manager.get_global_macros()
        for macro in macros:
            name = macro.get("name", "")
            self.preset_list_widget.addItem(name)
    
    def _on_preset_new(self):
        """新規プリセットを作成"""
        dialog = PresetEditDialog(self, config_manager=self.config_manager)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._load_presets()
    
    def _on_preset_edit(self):
        """プリセットを編集"""
        current_item = self.preset_list_widget.currentItem()
        if not current_item:
            QMessageBox.information(self, "編集", "編集するプリセットを選択してください。")
            return
        
        preset_name = current_item.text()
        if not self.config_manager:
            return
        
        macro = self.config_manager.get_macro_by_name(preset_name)
        if not macro:
            QMessageBox.warning(self, "エラー", f"プリセット '{preset_name}' が見つかりません。")
            return
        
        dialog = PresetEditDialog(
            self,
            config_manager=self.config_manager,
            preset_name=preset_name,
            commands=macro.get("commands", []),
            description=macro.get("description", "")
        )
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._load_presets()
    
    def _on_preset_delete(self):
        """プリセットを削除"""
        current_item = self.preset_list_widget.currentItem()
        if not current_item:
            QMessageBox.information(self, "削除", "削除するプリセットを選択してください。")
            return
        
        preset_name = current_item.text()
        
        reply = QMessageBox.question(
            self,
            "確認",
            f"プリセット '{preset_name}' を削除しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            if self.config_manager and self.config_manager.remove_global_macro(preset_name):
                self._load_presets()
                QMessageBox.information(self, "成功", f"プリセット '{preset_name}' を削除しました。")
            else:
                QMessageBox.warning(self, "エラー", "プリセットの削除に失敗しました。")
    
    def _on_keepalive_start(self):
        """キープアライブ開始"""
        interval = self.keepalive_interval_spin.value()
        self.keepalive_start_requested.emit(interval)
        self._update_keepalive_ui(True)
    
    def _on_keepalive_stop(self):
        """キープアライブ停止"""
        self.keepalive_stop_requested.emit()
        self._update_keepalive_ui(False)
    
    def _update_keepalive_ui(self, active: bool):
        """キープアライブUIを更新"""
        self.keepalive_active = active
        self.keepalive_start_btn.setEnabled(not active)
        self.keepalive_stop_btn.setEnabled(active)
        self.keepalive_interval_spin.setEnabled(not active)
        
        if active:
            interval = self.keepalive_interval_spin.value()
            self.keepalive_status_label.setText(f"状態: 動作中（{interval}秒間隔）")
        else:
            self.keepalive_status_label.setText("状態: 停止中")
    
class PresetEditDialog(QDialog):
    """プリセット編集ダイアログ"""
    
    def __init__(self, parent=None, config_manager=None,
                 preset_name: str = "", commands: List[str] = None,
                 description: str = ""):
        super().__init__(parent)
        self.config_manager = config_manager
        self.preset_name = preset_name
        self.is_new = (preset_name == "")
        
        self.setWindowTitle("プリセット編集" if preset_name else "プリセット新規作成")
        self.setMinimumWidth(500)
        self.setMinimumHeight(400)
        
        self._create_ui()
        
        # 既存データを設定
        if preset_name:
            self.name_edit.setText(preset_name)
            self.name_edit.setReadOnly(True)  # 名前は変更不可
        
        if commands:
            self.command_text.setPlainText('\n'.join(commands))
        
        if description:
            self.description_edit.setPlainText(description)
    
    def _create_ui(self):
        """UI作成"""
        layout = QVBoxLayout(self)
        
        # プリセット名
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("プリセット名:"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例: Cisco基本確認")
        name_layout.addWidget(self.name_edit)
        layout.addLayout(name_layout)
        
        # 説明
        layout.addWidget(QLabel("説明:"))
        self.description_edit = QTextEdit()
        self.description_edit.setMaximumHeight(80)
        self.description_edit.setPlaceholderText("このプリセットの説明を入力してください（省略可）")
        layout.addWidget(self.description_edit)
        
        # コマンドリスト
        cmd_layout = QVBoxLayout()
        
        label_layout = QHBoxLayout()
        label_layout.addWidget(QLabel("コマンドリスト:"))
        label_layout.addWidget(QLabel("（1行に1コマンド、コピペ可）"))
        label_layout.addStretch()
        cmd_layout.addLayout(label_layout)
        
        self.command_text = QTextEdit()
        self.command_text.setPlaceholderText(
            "コマンドを1行ずつ入力してください\n"
            "例:\n"
            "show running-config\n"
            "show ip interface brief\n"
            "show version"
        )
        cmd_layout.addWidget(self.command_text)
        
        layout.addLayout(cmd_layout)
        
        # ボタン
        button_layout = QHBoxLayout()
        
        save_btn = QPushButton("保存")
        save_btn.clicked.connect(self._on_save)
        button_layout.addWidget(save_btn)
        
        cancel_btn = QPushButton("キャンセル")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)
        
        layout.addLayout(button_layout)
    
    def _on_save(self):
        """保存"""
        # 入力チェック
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "エラー", "プリセット名を入力してください。")
            return
        
        # コマンドリストを取得（テキストエリアから1行ずつ）
        text = self.command_text.toPlainText()
        commands = [line.strip() for line in text.split('\n') if line.strip()]
        
        if not commands:
            QMessageBox.warning(self, "エラー", "コマンドを1つ以上追加してください。")
            return
        
        description = self.description_edit.toPlainText().strip()
        
        # 保存
        if not self.config_manager:
            QMessageBox.warning(self, "エラー", "設定管理が利用できません。")
            return
        
        if self.is_new:
            # 新規作成。add_global_macro は重複でも保存失敗でも False を返すので、
            # 重複はここで先に確かめる（保存失敗を名前のせいだと伝えない）
            if self.config_manager.get_macro_by_name(name):
                QMessageBox.warning(self, "エラー", f"プリセット '{name}' は既に存在します。")
            elif self.config_manager.add_global_macro(name, commands, description):
                QMessageBox.information(self, "成功", f"プリセット '{name}' を作成しました。")
                self.accept()
            else:
                QMessageBox.warning(
                    self, "エラー",
                    f"プリセット '{name}' を保存できませんでした。\n"
                    "設定ファイル (config.json) に書き込めるか確認してください。")
        else:
            # 更新
            if self.config_manager.update_global_macro(name, commands, description):
                QMessageBox.information(self, "成功", f"プリセット '{name}' を更新しました。")
                self.accept()
            else:
                QMessageBox.warning(self, "エラー", "プリセットの更新に失敗しました。")