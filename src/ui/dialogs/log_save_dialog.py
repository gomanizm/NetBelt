from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QProgressBar, QPushButton, QHBoxLayout
from PyQt6.QtCore import Qt, QThread, pyqtSignal


class LogSaveWorker(QThread):
    """ログ保存を別スレッドで実行するワーカー"""
    
    progress = pyqtSignal(int)  # 進行状況（0-100）
    finished = pyqtSignal(bool, str)  # 完了（成功/失敗、エラーメッセージ）
    
    def __init__(self, log_text: str, file_path: str):
        super().__init__()
        self.log_text = log_text
        self.file_path = file_path
        self._is_cancelled = False
    
    def cancel(self):
        """保存をキャンセル"""
        self._is_cancelled = True
    
    def run(self):
        """ログ保存を実行"""
        try:
            # ファイルに書き込み
            total_size = len(self.log_text)
            chunk_size = max(1024, total_size // 100)  # 最低1KB、最大100チャンク
            
            with open(self.file_path, 'w', encoding='utf-8') as f:
                for i in range(0, total_size, chunk_size):
                    if self._is_cancelled:
                        self.finished.emit(False, "キャンセルされました")
                        return
                    
                    chunk = self.log_text[i:i + chunk_size]
                    f.write(chunk)
                    
                    # 進行状況を更新
                    progress_percent = min(100, int((i + chunk_size) / total_size * 100))
                    self.progress.emit(progress_percent)
            
            self.finished.emit(True, "")
            
        except Exception as e:
            self.finished.emit(False, str(e))


class LogSaveProgressDialog(QDialog):
    """ログ保存プログレスダイアログ"""
    
    def __init__(self, log_text: str, file_path: str, parent=None):
        super().__init__(parent)
        self.log_text = log_text
        self.file_path = file_path
        self.worker = None
        self._success = False
        
        self.setWindowTitle("ログ保存中")
        self.setModal(True)
        self.setMinimumWidth(400)
        
        self._create_ui()
        self._start_save()
    
    def _create_ui(self):
        """UI作成"""
        layout = QVBoxLayout(self)
        
        # メッセージラベル
        self.message_label = QLabel("ログファイルを保存しています...")
        layout.addWidget(self.message_label)
        
        # ファイルパスラベル
        self.path_label = QLabel(self.file_path)
        self.path_label.setWordWrap(True)
        from ui import theme
        self.path_label.setStyleSheet(
            theme.dim_style(self, font_size="9pt"))
        layout.addWidget(self.path_label)
        
        # プログレスバー
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        
        # ボタンレイアウト
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        # キャンセルボタン
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.clicked.connect(self._on_cancel)
        button_layout.addWidget(self.cancel_button)
        
        layout.addLayout(button_layout)
    
    def _start_save(self):
        """保存処理を開始"""
        self.worker = LogSaveWorker(self.log_text, self.file_path)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()
    
    def _on_progress(self, percent: int):
        """進行状況を更新"""
        self.progress_bar.setValue(percent)
    
    def _on_finished(self, success: bool, error_message: str):
        """保存完了時の処理"""
        if success:
            self._success = True
            self.message_label.setText("保存完了!")
            self.cancel_button.setText("閉じる")
            self.accept()
        else:
            self.message_label.setText(f"保存失敗: {error_message}")
            self.cancel_button.setText("閉じる")
    
    def _on_cancel(self):
        """キャンセルボタンクリック時の処理"""
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait()
        self.reject()
    
    def exec(self) -> bool:
        """
        ダイアログを表示して保存処理を実行
        
        Returns:
            bool: 保存が成功した場合True
        """
        result = super().exec()
        return self._success
