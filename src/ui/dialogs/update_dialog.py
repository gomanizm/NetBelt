"""
更新ダイアログ
"""

import os
import sys
import subprocess
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QTextEdit, QWidget, QMessageBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from core.version_manager import VersionManager


class DownloadThread(QThread):
    """ダウンロードを別スレッドで実行"""
    
    progress_updated = pyqtSignal(int, int, int)  # 進捗%, ダウンロード済み, 総サイズ
    download_completed = pyqtSignal(str)  # ZIPファイルパス
    download_failed = pyqtSignal(str)  # エラーメッセージ
    
    def __init__(self, url: str, github_token: str = None, parent=None,
                 sha256_url: str = None):
        super().__init__(parent)
        self.url = url
        self.sha256_url = sha256_url
        self._cancelled = False
        self.version_mgr = VersionManager(github_token=github_token)
    
    def run(self):
        """ダウンロード実行"""
        try:
            zip_path = self.version_mgr.download_update(
                self.url,
                progress_callback=self._on_progress,
                sha256_url=self.sha256_url,
                cancel_check=lambda: self._cancelled
            )
            
            if zip_path:
                self.download_completed.emit(zip_path)
            else:
                self.download_failed.emit(
                    "ダウンロードに失敗しました"
                    "（チェックサムが一致しない場合も含みます）")
        
        except Exception as e:
            self.download_failed.emit(f"エラー: {str(e)}")
    
    def cancel(self):
        """ダウンロードの中止を要求する。

        terminate() は任意の位置でスレッドを殺すため、書きかけのファイルを
        残してしまう。ここではフラグを立て、ダウンロードループ側に
        後始末をさせてから抜けてもらう。
        """
        self._cancelled = True

    def _on_progress(self, progress: int, downloaded: int, total: int):
        """プログレス更新"""
        self.progress_updated.emit(progress, downloaded, total)


class UpdateDialog(QDialog):
    """更新ダイアログ"""
    
    # 戻り値の定数
    UPDATE_NOW = 1
    UPDATE_LATER = 2
    SKIP_VERSION = 3
    
    def __init__(self, parent, update_info: dict):
        """
        初期化
        
        Args:
            parent: 親ウィジェット
            update_info: 更新情報
                {
                    'version': str,
                    'release_notes': str,
                    'download_url': str,
                    'published_at': str
                }
        """
        super().__init__(parent)
        
        self.update_info = update_info
        self.downloaded_zip_path = None
        self.download_thread = None
        
        self.setWindowTitle("更新プログラム利用可能")
        self.setMinimumWidth(500)
        self.setMinimumHeight(400)
        
        # UI構築
        self._setup_ui()
        
        # 初期状態は通知フェーズ
        self._show_notification_phase()
    
    def _setup_ui(self):
        """UI構築"""
        layout = QVBoxLayout(self)
        
        # タイトルラベル
        self.title_label = QLabel()
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self.title_label.font()
        font.setPointSize(12)
        font.setBold(True)
        self.title_label.setFont(font)
        layout.addWidget(self.title_label)
        
        # バージョン情報ラベル
        try:
            from __version__ import __version__
            current_version = __version__
        except ImportError:
            current_version = "1.0.0"
        new_version = self.update_info.get('version', '不明')
        
        self.version_label = QLabel(f"現在: v{current_version}  →  新規: v{new_version}")
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.version_label)
        
        layout.addSpacing(10)
        
        # リリースノートラベル
        notes_label = QLabel("リリースノート:")
        layout.addWidget(notes_label)
        
        # リリースノート表示エリア
        self.notes_text = QTextEdit()
        self.notes_text.setReadOnly(True)
        self.notes_text.setPlainText(self.update_info.get('release_notes', 'リリースノートがありません'))
        self.notes_text.setMaximumHeight(150)
        layout.addWidget(self.notes_text)
        
        layout.addSpacing(10)
        
        # プログレスバー（初期は非表示）
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        
        # ダウンロード情報ラベル（初期は非表示）
        self.download_info_label = QLabel()
        self.download_info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.download_info_label.setVisible(False)
        layout.addWidget(self.download_info_label)
        
        # ステータスラベル
        self.status_label = QLabel()
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_label)
        
        layout.addSpacing(10)
        
        # ボタンコンテナ
        self.button_container = QWidget()
        self.button_layout = QHBoxLayout(self.button_container)
        self.button_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.button_container)
        
        # ボタン作成
        self.download_button = QPushButton("今すぐダウンロード")
        self.download_button.clicked.connect(self._on_download_clicked)
        
        self.later_button = QPushButton("後で")
        self.later_button.clicked.connect(self._on_later_clicked)
        
        self.skip_button = QPushButton("スキップ")
        self.skip_button.clicked.connect(self._on_skip_clicked)
        
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        
        self.apply_button = QPushButton("更新を適用")
        self.apply_button.clicked.connect(self._on_apply_clicked)
    
    def _show_notification_phase(self):
        """フェーズ1: 更新通知を表示"""
        self.title_label.setText("更新プログラム利用可能")
        self.status_label.setText("")
        
        # ボタンを配置
        self._clear_buttons()
        self.button_layout.addWidget(self.download_button)
        self.button_layout.addWidget(self.later_button)
        self.button_layout.addWidget(self.skip_button)
        
        self.download_button.setVisible(True)
        self.later_button.setVisible(True)
        self.skip_button.setVisible(True)
    
    def _show_downloading_phase(self):
        """フェーズ2: ダウンロード中を表示"""
        self.title_label.setText("更新プログラムをダウンロード中...")
        
        # プログレスバーを表示
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        
        self.download_info_label.setVisible(True)
        self.download_info_label.setText("0 B / 0 B")
        
        self.status_label.setText("ダウンロード中...")
        
        # ボタンを配置
        self._clear_buttons()
        self.button_layout.addStretch()
        self.button_layout.addWidget(self.cancel_button)
        self.button_layout.addStretch()
        
        self.cancel_button.setVisible(True)
    
    def _show_completed_phase(self):
        """フェーズ3: ダウンロード完了を表示"""
        self.title_label.setText("ダウンロード完了！")
        
        # プログレスバーを100%に
        self.progress_bar.setValue(100)
        
        self.status_label.setText(
            "✅ ダウンロード完了\n\n"
            "更新を適用するとアプリケーションが再起動されます。"
        )
        
        # ボタンを配置
        self._clear_buttons()
        self.button_layout.addStretch()
        self.button_layout.addWidget(self.apply_button)
        self.button_layout.addStretch()
        
        self.apply_button.setVisible(True)
    
    def _clear_buttons(self):
        """ボタンをすべて非表示にしてレイアウトからクリア"""
        while self.button_layout.count():
            item = self.button_layout.takeAt(0)
            if item.widget():
                item.widget().setVisible(False)
    
    def _on_download_clicked(self):
        """ダウンロードボタンがクリックされた"""
        download_url = self.update_info.get('download_url')
        
        if not download_url:
            QMessageBox.warning(
                self,
                "エラー",
                "ダウンロードURLが見つかりません。"
            )
            return
        
        # ダウンロードフェーズに移行
        self._show_downloading_phase()
        
        # ダウンロードスレッドを開始（トークンを渡す）
        from core.config_manager import ConfigManager
        config_mgr = ConfigManager()
        github_token = config_mgr.get_github_token()
        
        self.download_thread = DownloadThread(
            download_url, github_token, self,
            sha256_url=self.update_info.get('sha256_url'))
        self.download_thread.progress_updated.connect(self._on_progress_updated)
        self.download_thread.download_completed.connect(self._on_download_completed)
        self.download_thread.download_failed.connect(self._on_download_failed)
        self.download_thread.start()
    
    def _on_progress_updated(self, progress: int, downloaded: int, total: int):
        """ダウンロード進捗更新"""
        self.progress_bar.setValue(progress)
        
        downloaded_str = VersionManager.format_file_size(downloaded)
        total_str = VersionManager.format_file_size(total)
        
        self.download_info_label.setText(f"{downloaded_str} / {total_str}")
    
    def _on_download_completed(self, zip_path: str):
        """ダウンロード完了"""
        self.downloaded_zip_path = zip_path
        self._show_completed_phase()
    
    def _on_download_failed(self, error_message: str):
        """ダウンロード失敗"""
        QMessageBox.critical(
            self,
            "ダウンロードエラー",
            f"ダウンロードに失敗しました。\n\n{error_message}"
        )
        self.reject()
    
    def _on_apply_clicked(self):
        """更新適用ボタンがクリックされた"""
        if not self.downloaded_zip_path or not os.path.exists(self.downloaded_zip_path):
            QMessageBox.warning(
                self,
                "エラー",
                "更新ファイルが見つかりません。"
            )
            return
        
        # updater.batのパスを取得
        if getattr(sys, 'frozen', False):
            # PyInstallerでビルドされている場合
            app_dir = os.path.dirname(sys.executable)
            app_path = sys.executable
        else:
            # 開発環境の場合
            # __file__ = <アプリのルート>\src\ui\dialogs\update_dialog.py
            # 3回上がって src/ に、さらに1回上がってアプリのルートに
            app_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            app_path = sys.executable
        
        updater_path = os.path.join(app_dir, "updater.bat")
        
        # デバッグ情報を表示
        print(f"[UpdateDialog] app_dir: {app_dir}")
        print(f"[UpdateDialog] updater_path: {updater_path}")
        print(f"[UpdateDialog] zip_path: {self.downloaded_zip_path}")
        print(f"[UpdateDialog] app_path: {app_path}")
        print(f"[UpdateDialog] updater.bat exists: {os.path.exists(updater_path)}")
        
        # updater.batの存在確認
        if not os.path.exists(updater_path):
            QMessageBox.critical(
                self,
                "エラー",
                f"updater.batが見つかりません。\n\nパス: {updater_path}"
            )
            return
        
        # updater.batを起動
        try:
            # updater.bat <ZIPパス> <実行ファイルパス>
            subprocess.Popen(
                [updater_path, self.downloaded_zip_path, app_path],
                creationflags=subprocess.CREATE_NEW_CONSOLE
            )
            
            # ダイアログを閉じる
            self.done(self.UPDATE_NOW)
            
            # アプリケーションを終了
            from PyQt6.QtWidgets import QApplication
            QApplication.quit()
        
        except Exception as e:
            QMessageBox.critical(
                self,
                "エラー",
                f"更新プロセスの起動に失敗しました。\n\n{str(e)}"
            )
    
    def _on_later_clicked(self):
        """後でボタンがクリックされた"""
        self.done(self.UPDATE_LATER)
    
    def _on_skip_clicked(self):
        """スキップボタンがクリックされた"""
        reply = QMessageBox.question(
            self,
            "確認",
            f"バージョン v{self.update_info.get('version')} をスキップしますか？\n\n"
            "次のバージョンがリリースされるまで、この更新は表示されません。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self.done(self.SKIP_VERSION)
    
    def _on_cancel_clicked(self):
        """キャンセルボタンがクリックされた（ダウンロード中）"""
        reply = QMessageBox.question(
            self,
            "確認",
            "ダウンロードをキャンセルしますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            # スレッドを停止
            if self.download_thread and self.download_thread.isRunning():
                self.download_thread.cancel()
                self.download_thread.wait(10000)
            
            self.reject()
    
    def closeEvent(self, event):
        """ダイアログが閉じられる前の処理"""
        # ダウンロード中の場合は確認
        if self.download_thread and self.download_thread.isRunning():
            reply = QMessageBox.question(
                self,
                "確認",
                "ダウンロード中です。本当に閉じますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
            
            # スレッドを停止
            self.download_thread.cancel()
            self.download_thread.wait(10000)
        
        event.accept()