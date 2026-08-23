"""SFTPファイルブラウザパネル"""
import os
from datetime import datetime
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeView, QToolBar,
    QPushButton, QLabel, QProgressBar, QMenu, QMessageBox,
    QFileDialog, QInputDialog, QHeaderView
)
from PyQt6.QtCore import Qt, QModelIndex, pyqtSignal
from PyQt6.QtGui import QStandardItemModel, QStandardItem, QIcon, QAction, QDragEnterEvent, QDropEvent
from typing import Optional
from core.sftp_manager import SFTPManager


class SFTPPanel(QWidget):
    """SFTPファイルブラウザパネル"""
    
    def __init__(self, parent=None):
        """
        初期化
        
        Args:
            parent: 親ウィジェット
        """
        super().__init__(parent)
        
        self.sftp_manager: Optional[SFTPManager] = None
        self.current_device = ""
        
        # UI初期化
        self._init_ui()
        
        # ドラッグ&ドロップを有効化
        self.setAcceptDrops(True)
    
    def _init_ui(self):
        """UIを初期化"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        
        # ツールバー
        toolbar = self._create_toolbar()
        layout.addWidget(toolbar)
        
        # 現在のパス表示
        self.path_label = QLabel("接続されていません")
        self.path_label.setStyleSheet("background-color: #f0f0f0; padding: 5px; border: 1px solid #ccc;")
        layout.addWidget(self.path_label)
        
        # ファイルリストビュー
        self.tree_view = QTreeView()
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_view.customContextMenuRequested.connect(self._show_context_menu)
        self.tree_view.doubleClicked.connect(self._on_item_double_clicked)
        
        # モデル作成
        self.model = QStandardItemModel()
        self.model.setHorizontalHeaderLabels(["名前", "サイズ", "パーミッション", "更新日時"])
        self.tree_view.setModel(self.model)
        
        # カラム幅を調整
        header = self.tree_view.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        
        layout.addWidget(self.tree_view)
        
        # プログレスバー
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        
        # ステータスラベル
        self.status_label = QLabel("")
        layout.addWidget(self.status_label)
    
    def _create_toolbar(self) -> QToolBar:
        """ツールバーを作成"""
        toolbar = QToolBar()
        toolbar.setMovable(False)
        
        # 更新ボタン
        refresh_action = QAction("更新", self)
        refresh_action.triggered.connect(self._on_refresh)
        toolbar.addAction(refresh_action)
        
        # 親ディレクトリへ移動
        up_action = QAction("↑ 親へ", self)
        up_action.triggered.connect(self._on_go_up)
        toolbar.addAction(up_action)
        
        # ホームディレクトリへ移動
        home_action = QAction("ホーム", self)
        home_action.triggered.connect(self._on_go_home)
        toolbar.addAction(home_action)
        
        toolbar.addSeparator()
        
        # アップロードボタン
        upload_action = QAction("アップロード", self)
        upload_action.triggered.connect(self._on_upload)
        toolbar.addAction(upload_action)
        
        # ダウンロードボタン
        download_action = QAction("ダウンロード", self)
        download_action.triggered.connect(self._on_download)
        toolbar.addAction(download_action)
        
        toolbar.addSeparator()
        
        # 新規ディレクトリ
        mkdir_action = QAction("新規フォルダ", self)
        mkdir_action.triggered.connect(self._on_create_directory)
        toolbar.addAction(mkdir_action)
        
        # 削除ボタン
        delete_action = QAction("削除", self)
        delete_action.triggered.connect(self._on_delete)
        toolbar.addAction(delete_action)
        
        return toolbar
    
    def set_sftp_manager(self, sftp_manager: SFTPManager, device_name: str = ""):
        """
        SFTPマネージャーを設定
        
        Args:
            sftp_manager: SFTPマネージャー
            device_name: デバイス名
        """
        # 既存の接続を解除
        if self.sftp_manager:
            try:
                self.sftp_manager.file_list_ready.disconnect()
                self.sftp_manager.transfer_progress.disconnect()
                self.sftp_manager.transfer_complete.disconnect()
                self.sftp_manager.error_occurred.disconnect()
            except Exception:
                pass
        
        self.sftp_manager = sftp_manager
        self.current_device = device_name
        
        # シグナル接続
        self.sftp_manager.file_list_ready.connect(self._update_file_list)
        self.sftp_manager.transfer_progress.connect(self._update_progress)
        self.sftp_manager.transfer_complete.connect(self._on_transfer_complete)
        self.sftp_manager.error_occurred.connect(self._on_error)
        
        # 初期ディレクトリ一覧を取得
        self.sftp_manager.list_directory()
    
    def clear(self):
        """パネルをクリア"""
        self.model.removeRows(0, self.model.rowCount())
        self.path_label.setText("接続されていません")
        self.status_label.setText("")
        self.progress_bar.setVisible(False)
        self.sftp_manager = None
        self.current_device = ""
    
    def _update_file_list(self, file_list: list):
        """
        ファイル一覧を更新
        
        Args:
            file_list: ファイル情報のリスト
        """
        # モデルをクリア
        self.model.removeRows(0, self.model.rowCount())
        
        # 現在のパスを表示
        if self.sftp_manager:
            current_path = self.sftp_manager.get_current_path()
            self.path_label.setText(f"現在のパス: {current_path}")
        
        # ファイル/ディレクトリを追加
        for file_info in file_list:
            name_item = QStandardItem(file_info['name'])
            
            # ディレクトリにはアイコンを付ける（簡易的に[DIR]を前置）
            if file_info['is_dir']:
                name_item.setText(f"📁 {file_info['name']}")
            else:
                name_item.setText(f"📄 {file_info['name']}")
            
            # サイズ
            if file_info['is_dir']:
                size_item = QStandardItem("")
            else:
                size_item = QStandardItem(self._format_size(file_info['size']))
            
            # パーミッション
            perm_item = QStandardItem(file_info['permissions'])
            
            # 更新日時
            mtime = datetime.fromtimestamp(file_info['mtime'])
            time_item = QStandardItem(mtime.strftime("%Y-%m-%d %H:%M:%S"))
            
            # データとして元のファイル情報を保持
            name_item.setData(file_info, Qt.ItemDataRole.UserRole)
            
            # 行を追加
            self.model.appendRow([name_item, size_item, perm_item, time_item])
        
        self.status_label.setText(f"{len(file_list)} 項目")
    
    def _update_progress(self, transferred: int, total: int):
        """
        転送進捗を更新
        
        Args:
            transferred: 転送済みバイト数
            total: 全体バイト数
        """
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(transferred)
        
        # パーセンテージを表示
        if total > 0:
            percent = int(transferred * 100 / total)
            self.progress_bar.setFormat(f"{percent}% ({self._format_size(transferred)} / {self._format_size(total)})")
    
    def _on_transfer_complete(self, message: str):
        """
        転送完了時の処理
        
        Args:
            message: 完了メッセージ
        """
        self.progress_bar.setVisible(False)
        self.status_label.setText(message)
    
    def _on_error(self, error_message: str):
        """
        エラー発生時の処理
        
        Args:
            error_message: エラーメッセージ
        """
        self.progress_bar.setVisible(False)
        self.status_label.setText(f"エラー: {error_message}")
        QMessageBox.warning(self, "SFTP エラー", error_message)
    
    def _on_item_double_clicked(self, index: QModelIndex):
        """
        アイテムがダブルクリックされた
        
        Args:
            index: クリックされたインデックス
        """
        if not self.sftp_manager:
            return
        
        # ファイル情報を取得
        name_item = self.model.item(index.row(), 0)
        file_info = name_item.data(Qt.ItemDataRole.UserRole)
        
        # ディレクトリの場合は移動
        if file_info and file_info['is_dir']:
            current_path = self.sftp_manager.get_current_path()
            new_path = f"{current_path}/{file_info['name']}" if current_path != "/" else f"/{file_info['name']}"
            self.sftp_manager.change_directory(new_path)
    
    def _show_context_menu(self, position):
        """
        右クリックメニューを表示
        
        Args:
            position: メニュー表示位置
        """
        if not self.sftp_manager:
            return
        
        # 選択されているアイテムを取得
        index = self.tree_view.indexAt(position)
        
        menu = QMenu(self)
        
        if index.isValid():
            # アイテムが選択されている場合
            name_item = self.model.item(index.row(), 0)
            file_info = name_item.data(Qt.ItemDataRole.UserRole)
            
            if file_info['is_dir']:
                # ディレクトリの場合
                open_action = menu.addAction("開く")
                open_action.triggered.connect(lambda: self._on_item_double_clicked(index))
            else:
                # ファイルの場合
                download_action = menu.addAction("ダウンロード")
                download_action.triggered.connect(lambda: self._on_download_selected(file_info))
            
            menu.addSeparator()
            
            # 共通メニュー
            rename_action = menu.addAction("名前変更")
            rename_action.triggered.connect(lambda: self._on_rename_selected(file_info))
            
            delete_action = menu.addAction("削除")
            delete_action.triggered.connect(lambda: self._on_delete_selected(file_info))
            
            menu.addSeparator()
            
            chmod_action = menu.addAction("パーミッション変更")
            chmod_action.triggered.connect(lambda: self._on_chmod_selected(file_info))
        else:
            # 空白部分の場合
            upload_action = menu.addAction("ファイルをアップロード")
            upload_action.triggered.connect(self._on_upload)
            
            menu.addSeparator()
            
            mkdir_action = menu.addAction("新規フォルダ作成")
            mkdir_action.triggered.connect(self._on_create_directory)
            
            menu.addSeparator()
            
            refresh_action = menu.addAction("更新")
            refresh_action.triggered.connect(self._on_refresh)
        
        menu.exec(self.tree_view.viewport().mapToGlobal(position))
    
    def _on_refresh(self):
        """更新ボタンがクリックされた"""
        if self.sftp_manager:
            self.sftp_manager.list_directory()
    
    def _on_go_up(self):
        """親ディレクトリへ移動"""
        if self.sftp_manager:
            parent_path = self.sftp_manager.get_parent_directory()
            self.sftp_manager.change_directory(parent_path)
    
    def _on_go_home(self):
        """ホームディレクトリへ移動"""
        if self.sftp_manager:
            self.sftp_manager.change_directory(".")
    
    def _on_upload(self):
        """アップロードボタンがクリックされた"""
        if not self.sftp_manager:
            return
        
        # ファイル選択ダイアログ
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "アップロードするファイルを選択",
            "",
            "すべてのファイル (*.*)"
        )
        
        if file_path:
            self.sftp_manager.upload_file(file_path)
    
    def _on_download(self):
        """ダウンロードボタンがクリックされた"""
        # 選択されているアイテムを取得
        indexes = self.tree_view.selectedIndexes()
        if not indexes:
            QMessageBox.information(self, "ダウンロード", "ダウンロードするファイルを選択してください。")
            return
        
        index = indexes[0]
        name_item = self.model.item(index.row(), 0)
        file_info = name_item.data(Qt.ItemDataRole.UserRole)
        
        if file_info['is_dir']:
            QMessageBox.information(self, "ダウンロード", "ディレクトリはダウンロードできません。")
            return
        
        self._on_download_selected(file_info)
    
    def _on_download_selected(self, file_info: dict):
        """
        選択されたファイルをダウンロード
        
        Args:
            file_info: ファイル情報
        """
        if not self.sftp_manager:
            return
        
        # 保存先を選択
        local_path, _ = QFileDialog.getSaveFileName(
            self,
            "ファイルを保存",
            file_info['name'],
            "すべてのファイル (*.*)"
        )
        
        if local_path:
            current_path = self.sftp_manager.get_current_path()
            remote_path = f"{current_path}/{file_info['name']}" if current_path != "/" else f"/{file_info['name']}"
            self.sftp_manager.download_file(remote_path, local_path)
    
    def _on_create_directory(self):
        """新規ディレクトリ作成"""
        if not self.sftp_manager:
            return
        
        # ディレクトリ名を入力
        dir_name, ok = QInputDialog.getText(
            self,
            "新規フォルダ作成",
            "フォルダ名を入力してください:"
        )
        
        if ok and dir_name:
            current_path = self.sftp_manager.get_current_path()
            new_path = f"{current_path}/{dir_name}" if current_path != "/" else f"/{dir_name}"
            self.sftp_manager.create_directory(new_path)
    
    def _on_delete(self):
        """削除ボタンがクリックされた"""
        # 選択されているアイテムを取得
        indexes = self.tree_view.selectedIndexes()
        if not indexes:
            QMessageBox.information(self, "削除", "削除するアイテムを選択してください。")
            return
        
        index = indexes[0]
        name_item = self.model.item(index.row(), 0)
        file_info = name_item.data(Qt.ItemDataRole.UserRole)
        
        self._on_delete_selected(file_info)
    
    def _on_delete_selected(self, file_info: dict):
        """
        選択されたアイテムを削除
        
        Args:
            file_info: ファイル情報
        """
        if not self.sftp_manager:
            return
        
        # 確認ダイアログ
        reply = QMessageBox.question(
            self,
            "削除確認",
            f"'{file_info['name']}' を削除しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            current_path = self.sftp_manager.get_current_path()
            item_path = f"{current_path}/{file_info['name']}" if current_path != "/" else f"/{file_info['name']}"
            self.sftp_manager.delete_item(item_path, file_info['is_dir'])
    
    def _on_rename_selected(self, file_info: dict):
        """
        選択されたアイテムの名前を変更
        
        Args:
            file_info: ファイル情報
        """
        if not self.sftp_manager:
            return
        
        # 新しい名前を入力
        new_name, ok = QInputDialog.getText(
            self,
            "名前変更",
            "新しい名前を入力してください:",
            text=file_info['name']
        )
        
        if ok and new_name and new_name != file_info['name']:
            current_path = self.sftp_manager.get_current_path()
            old_path = f"{current_path}/{file_info['name']}" if current_path != "/" else f"/{file_info['name']}"
            new_path = f"{current_path}/{new_name}" if current_path != "/" else f"/{new_name}"
            self.sftp_manager.rename_item(old_path, new_path)
    
    def _on_chmod_selected(self, file_info: dict):
        """
        選択されたアイテムのパーミッションを変更
        
        Args:
            file_info: ファイル情報
        """
        if not self.sftp_manager:
            return
        
        # 現在のパーミッションを8進数で表示
        current_mode = file_info['mode'] & 0o777
        current_mode_str = oct(current_mode)[2:]  # '0o755' -> '755'
        
        # 新しいパーミッションを入力
        new_mode_str, ok = QInputDialog.getText(
            self,
            "パーミッション変更",
            f"新しいパーミッションを8進数で入力してください:\n（例: 755, 644）",
            text=current_mode_str
        )
        
        if ok and new_mode_str:
            try:
                # 8進数として解釈
                new_mode = int(new_mode_str, 8)
                current_path = self.sftp_manager.get_current_path()
                item_path = f"{current_path}/{file_info['name']}" if current_path != "/" else f"/{file_info['name']}"
                self.sftp_manager.change_permissions(item_path, new_mode)
            except ValueError:
                QMessageBox.warning(self, "入力エラー", "パーミッションは8進数で入力してください（例: 755）")
    
    def dragEnterEvent(self, event: QDragEnterEvent):
        """
        ドラッグエンター時の処理
        
        Args:
            event: ドラッグエンターイベント
        """
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
    
    def dropEvent(self, event: QDropEvent):
        """
        ドロップ時の処理（ファイルアップロード）
        
        Args:
            event: ドロップイベント
        """
        if not self.sftp_manager:
            return
        
        urls = event.mimeData().urls()
        for url in urls:
            file_path = url.toLocalFile()
            if os.path.isfile(file_path):
                self.sftp_manager.upload_file(file_path)
    
    @staticmethod
    def _format_size(size: int) -> str:
        """
        ファイルサイズを読みやすい形式にフォーマット
        
        Args:
            size: バイト数
            
        Returns:
            str: フォーマットされたサイズ文字列
        """
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"