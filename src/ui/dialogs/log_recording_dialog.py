from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from datetime import datetime
import os
from ui import theme


class LogRecordingDialog(QDialog):
    """ログ記録中に表示するダイアログ"""
    
    # 停止要求シグナル（どの機器のダイアログかを必ず伝える）。
    # 機器名を載せないと、受け手は「表示中のタブ」を止めるしかなく、
    # 2台を同時に記録しているときに別の機器の記録を打ち切ってしまう。
    stop_requested = pyqtSignal(str)
    
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
        self.timer.timeout.connect(self._update_byte_count)
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
        path_label.setStyleSheet(theme.dim_style(self, font_size="9pt"))
        layout.addWidget(path_label)
        
        # 経過時間ラベル
        self.elapsed_label = QLabel("経過時間: 00:00:00")
        self.elapsed_label.setStyleSheet("font-size: 10pt;")
        layout.addWidget(self.elapsed_label)

        # 記録したバイト数（本当に書けているかを見えるようにする）
        self.bytes_label = QLabel()
        self.bytes_label.setStyleSheet("font-size: 10pt;")
        layout.addWidget(self.bytes_label)
        self._update_byte_count()
        
        # 注意事項ラベル
        note_label = QLabel(
            "※ ログは受信したデータがリアルタイムで保存されます。\n"
            "※ 記録を停止するには、下のボタンをクリックしてください。"
        )
        note_label.setStyleSheet(
            theme.dim_style(self, font_size="9pt") + " margin-top: 10px;")
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
        """経過時間を更新

        timedelta.seconds は days を含まない 0..86399 の値なので、そのまま
        時・分・秒へ割ると 24 時間ごとに表示だけが巻き戻る。総秒数から
        組み立て、24 時間を超えたぶんは hours へ繰り上げる。
        """
        elapsed = datetime.now() - self.start_time
        total_seconds = int(elapsed.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        self.elapsed_label.setText(f"経過時間: {hours:02d}:{minutes:02d}:{seconds:02d}")
    
    def _update_byte_count(self):
        """保存先ファイルの実際の大きさを表示する

        受信した文字数ではなくファイルの大きさを出す。テキストモードで
        開いているので改行は CRLF になり、日本語は UTF-8 で 3 バイトになる。
        書き込みのたびに flush しているので、ここで読む値が記録できた量。
        """
        try:
            size = os.path.getsize(self.file_path)
        except OSError:
            self.bytes_label.setText("記録したバイト数: 取得できません")
            return
        self.bytes_label.setText(f"記録したバイト数: {size:,} バイト")

    def _on_stop(self):
        """停止ボタンクリック時の処理"""
        self.timer.stop()
        self.stop_requested.emit(self.device_name)
        self.close()
    
    def closeEvent(self, event):
        """ダイアログを閉じる時の処理"""
        self.timer.stop()
        super().closeEvent(event)
