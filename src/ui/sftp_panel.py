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
    
    # settings.sftp の既定値。src/resources/default_config.json に合わせてある
    SFTP_SETTING_DEFAULTS = {
        "default_download_path": "./downloads",
        "show_hidden_files": False,
        "confirm_delete": True,
        "confirm_overwrite": True,
    }
    
    def __init__(self, parent=None, config_manager=None):
        """
        初期化
        
        Args:
            parent: 親ウィジェット
            config_manager: 設定マネージャ（settings.sftp を読むために使う。
                None のときは既定値で動く）
        """
        super().__init__(parent)
        
        self.config_manager = config_manager
        self.sftp_manager: Optional[SFTPManager] = None
        self.current_device = ""
        # リモートの現在のディレクトリにある名前 -> ディレクトリか否か。
        # 上書き確認の判定に使う。表示フィルタとは別に持つ（隠しファイルを
        # 非表示にしているだけで上書き保護が外れてはいけない）
        self._current_entries = {}
        # 送信を始めたがまだ一覧に現れていない名前。一覧が来たら捨てる
        self._pending_upload_names = set()
        
        # UI初期化
        self._init_ui()
        
        # ドラッグ&ドロップを有効化
        self.setAcceptDrops(True)
    
    @classmethod
    def normalize_sftp_settings(cls, settings) -> dict:
        """
        settings.sftp を検証し、妥当でないものを既定値で埋める
        
        config.json は手で編集できるため、型の違う値が入りうる。
        真偽値のつもりの "no" は文字列として真になり、null を
        QCheckBox.setChecked() へ渡すと例外になる。設定ダイアログも
        この結果を使う（生値を渡すとダイアログが開けなくなる）。
        
        Args:
            settings: settings.sftp 相当の dict（None や dict 以外も受け付ける）
        
        Returns:
            4キーすべてが妥当な値で埋まった dict
        """
        merged = dict(cls.SFTP_SETTING_DEFAULTS)
        if not isinstance(settings, dict):
            return merged
        
        path = settings.get("default_download_path")
        if isinstance(path, str) and path.strip():
            merged["default_download_path"] = path.strip()
        
        # 真偽値は bool のみ受理する。0/1 や "no" を通すと、
        # 画面の表示と実際の動作が食い違う
        for key in ("show_hidden_files", "confirm_delete", "confirm_overwrite"):
            value = settings.get(key)
            if isinstance(value, bool):
                merged[key] = value
        
        return merged
    
    def _get_sftp_setting(self, key: str, default):
        """
        settings.sftp から設定値を取得する
        
        Args:
            key: 設定キー
            default: 未使用（正規化後の既定値を使う。呼び出し側の可読性のために残す）
        
        Returns:
            正規化済みの設定値
        """
        raw = self.config_manager.get_server_settings("sftp") if self.config_manager else {}
        return self.normalize_sftp_settings(raw)[key]
    
    # 接続先が無いときの表示
    NO_TARGET_TEXT = "接続先: なし"

    # 未接続のときに出す案内。接続すると消す。
    HINT_TEXT = (
        "ターミナルで機器へ SSH 接続すると、このパネルが使えるようになります。\n"
        "SSH で入れても、機器が SFTP に対応していない場合は使えません。"
    )

    def _init_ui(self):
        """UIを初期化"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        
        # 接続先の明示。ツールバーより上に置く。どの機器を相手にしているかは、
        # ファイルを落とす前に必ず目に入るべき情報。
        self.target_label = QLabel(self.NO_TARGET_TEXT)
        self.target_label.setStyleSheet(
            "background-color: #eef4fb; border: 1px solid #b8cfe6;"
            " padding: 6px; font-weight: bold;")
        layout.addWidget(self.target_label)

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

        # 使い方の案内。このパネルはターミナルの SSH セッションに相乗りする
        # 設計で、単独で接続する手段が無い。黙っていて分かるものではない。
        self.hint_label = QLabel(self.HINT_TEXT)
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("color: #666; padding: 4px;")
        layout.addWidget(self.hint_label)
    
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
    
    def _detach_manager(self):
        """いま繋いでいるマネージャから、自分の接続だけを外す

        引数なしの disconnect() はそのシグナルの全接続を外してしまい、
        MainWindow が張ったエラー監視まで消える。スロットを指定して外す。

        外さずに参照だけ捨てると、旧マネージャの遅れた通知で
        空にしたはずのパネルが埋め直される。
        """
        if not self.sftp_manager:
            return
        for signal, slot in (
            (self.sftp_manager.file_list_ready, self._update_file_list),
            (self.sftp_manager.transfer_progress, self._update_progress),
            (self.sftp_manager.transfer_complete, self._on_transfer_complete),
            (self.sftp_manager.error_occurred, self._on_error),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass    # 繋がっていなければそれでよい

    def set_sftp_manager(self, sftp_manager: SFTPManager, device_name: str = "",
                         target: str = ""):
        """
        SFTPマネージャーを設定
        
        Args:
            sftp_manager: SFTPマネージャー
            device_name: デバイス名
        """
        # 既存の接続を解除（自分の分だけ）
        self._detach_manager()
        
        self.sftp_manager = sftp_manager
        self.hint_label.setVisible(False)
        # 接続先が変わるので、前の接続で観測した一覧は使えない
        self._current_entries = {}
        self._pending_upload_names = set()
        self.current_device = device_name
        
        # シグナル接続
        self.sftp_manager.file_list_ready.connect(self._update_file_list)
        self.sftp_manager.transfer_progress.connect(self._update_progress)
        self.sftp_manager.transfer_complete.connect(self._on_transfer_complete)
        self.sftp_manager.error_occurred.connect(self._on_error)
        
        # どの機器を見ているかを出す。出さないと、送り先が違っても気づけない。
        if target:
            self.target_label.setText("接続先: %s (%s)" % (device_name, target))
        elif device_name:
            self.target_label.setText("接続先: %s" % device_name)
        else:
            self.target_label.setText(self.NO_TARGET_TEXT)
        self._update_path_label(self.sftp_manager.current_path)

        # 初期ディレクトリ一覧を取得
        self.sftp_manager.list_directory()
    
    def _update_path_label(self, path: str) -> None:
        """パスの表示に機器名を添える"""
        if self.current_device:
            self.path_label.setText("%s: %s" % (self.current_device, path))
        else:
            self.path_label.setText("現在のパス: %s" % path)

    def clear(self):
        """パネルをクリア

        旧マネージャの通知が遅れて届くと、空にしたはずのパネルが
        旧機器の一覧で埋め直される。参照を捨てる前に接続を外す。
        """
        self._detach_manager()
        self.model.removeRows(0, self.model.rowCount())
        self.path_label.setText("接続されていません")
        self.target_label.setText(self.NO_TARGET_TEXT)
        self.hint_label.setVisible(True)
        self.status_label.setText("")
        self.progress_bar.setVisible(False)
        self.sftp_manager = None
        self.current_device = ""
        # 残しておくと、次の接続で一覧を取る前に古い名前で上書き判定してしまう
        self._current_entries = {}
        self._pending_upload_names = set()
    
    def _update_file_list(self, file_list: list):
        """
        ファイル一覧を更新
        
        Args:
            file_list: ファイル情報のリスト
        """
        # 上書き確認は「リモートに何があるか」の話なので、表示フィルタを
        # かける前の一覧から作る。隠しファイルを非表示にしているだけで
        # ドットファイルが無警告で上書きされてはいけない
        self._current_entries = {f['name']: bool(f['is_dir']) for f in file_list}
        # 新しい一覧が真実なので、送信中として覚えていた名前は捨てる
        self._pending_upload_names.clear()
        
        # 隠しファイルの扱い（settings.sftp.show_hidden_files）
        show_hidden = self._get_sftp_setting(
            "show_hidden_files", self.SFTP_SETTING_DEFAULTS["show_hidden_files"])
        if not show_hidden:
            file_list = [f for f in file_list if not f['name'].startswith('.')]
        
        # モデルをクリア
        self.model.removeRows(0, self.model.rowCount())
        
        # 現在のパスを表示
        if self.sftp_manager:
            # ここで直接書くと、機器名つきの表示を消してしまう
            self._update_path_label(self.sftp_manager.get_current_path())
        
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
            self._upload_with_confirmation(file_path)
    
    def _upload_with_confirmation(self, file_path: str):
        """
        上書き確認を挟んでアップロードする
        
        Args:
            file_path: ローカルのファイルパス
        """
        name = os.path.basename(file_path)
        confirm = self._get_sftp_setting(
            "confirm_overwrite", self.SFTP_SETTING_DEFAULTS["confirm_overwrite"])
        # 同名でもディレクトリなら上書きではなく単に失敗するので、
        # 「上書きしますか」とは聞かない。観測済みの種別が最優先で、
        # まだ一覧に現れていない送信中の名前も既存ファイルとして扱う
        known_is_dir = self._current_entries.get(name)
        if known_is_dir is True:
            overwrites_file = False
        else:
            overwrites_file = (known_is_dir is False
                               or name in self._pending_upload_names)
        if confirm and overwrites_file:
            reply = QMessageBox.question(
                self,
                "上書き確認",
                f"リモートに '{name}' が既にあります。上書きしますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        
        # アップロード完了後に sftp_manager が一覧を取り直すが、1回のドロップで
        # 複数送る間は間に合わない。送信中の名前を別に覚えておき、同じドロップ内の
        # 同名2件目以降にも確認が出るようにする。_current_entries は
        # 「最後に観測したリモートの一覧」のまま保つ（種別を汚さないため）
        self._pending_upload_names.add(name)
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
        
        # 保存先を選択（settings.sftp.default_download_path を初期位置に使う）
        download_dir = self._get_sftp_setting(
            "default_download_path",
            self.SFTP_SETTING_DEFAULTS["default_download_path"])
        suggested = os.path.join(download_dir, file_info['name'])
        
        local_path, _ = QFileDialog.getSaveFileName(
            self,
            "ファイルを保存",
            suggested,
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
        
        # 確認ダイアログ（settings.sftp.confirm_delete）
        if self._get_sftp_setting(
                "confirm_delete", self.SFTP_SETTING_DEFAULTS["confirm_delete"]):
            reply = QMessageBox.question(
                self,
                "削除確認",
                f"'{file_info['name']}' を削除しますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        
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
                self._upload_with_confirmation(file_path)
    
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