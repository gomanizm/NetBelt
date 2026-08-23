from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from datetime import datetime


class LogRecordingDialog(QDialog):
    """ログ記録中に表示するダイアログ"""
    
    stop_requested = pyqtSignal()  # 停止要求シグナル
    
    def __init__(self, device_name: str, file_path: str, parent=None):
        super().__init__(parent)
        self.device_name = device_name
        self.file_path = file_path
        self.start_time = datetime.now()
        
        self.setWindowTitle("ログ記録中")
        self.setModal(False)  # ノンモーダル
        self.setMinimumWidth(400)
        
        # タイマーで経過時間を更新
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_elapsed_time)
        self.timer.start(1000)  # 1秒ごとに更新
        
        self._create_ui()
    
    def _create_ui(self):
        """UI作成"""
        layout = QVBoxLayout(self)
        
        # タイトルラベル
        title_label = QLabel(f"【{self.device_name}】のログを記録中")
        title_label.setStyleSheet("font-weight: bold; font-size: 11pt;")
        layout.addWidget(title_label)
        
        # ファイルパスラベル
        path_label = QLabel(f"保存先: {self.file_path}")
        path_label.setWordWrap(True)
        path_label.setStyleSheet("color: gray; font-size: 9pt;")
        layout.addWidget(path_label)
        
        # 経過時間ラベル
        self.elapsed_label = QLabel("経過時間: 00:00:00")
        self.elapsed_label.setStyleSheet("font-size: 10pt;")
        layout.addWidget(self.elapsed_label)
        
        # 注意事項ラベル
        note_label = QLabel(
            "※ ログは受信したデータがリアルタイムで保存されます。\n"
            "※ 記録を停止するには、下のボタンをクリックしてください。"
        )
        note_label.setStyleSheet("color: #666; font-size: 9pt; margin-top: 10px;")
        note_label.setWordWrap(True)
        layout.addWidget(note_label)
        
        # ボタンレイアウト
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        # 停止ボタン
        stop_button = QPushButton("記録停止")
        stop_button.setStyleSheet("background-color: #d9534f; color: white; font-weight: bold; padding: 5px 15px;")
        stop_button.clicked.connect(self._on_stop)
        button_layout.addWidget(stop_button)
        
        layout.addLayout(button_layout)
    
    def _update_elapsed_time(self):
        """経過時間を更新"""
        elapsed = datetime.now() - self.start_time
        hours = elapsed.seconds // 3600
        minutes = (elapsed.seconds % 3600) // 60
        seconds = elapsed.seconds % 60
        self.elapsed_label.setText(f"経過時間: {hours:02d}:{minutes:02d}:{seconds:02d}")
    
    def _on_stop(self):
        """停止ボタンクリック時の処理"""
        self.timer.stop()
        self.stop_requested.emit()
        self.close()
    
    def closeEvent(self, event):
        """ダイアログを閉じる時の処理"""
        self.timer.stop()
        super().closeEvent(event)
