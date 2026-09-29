from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QKeySequence
from datetime import datetime
from ui import theme


class LogRecordingDialog(QDialog):
    """ログ記録中に表示するダイアログ"""
    
    # 停止要求シグナル（どの機器のダイアログかを必ず伝える）。
    # 機器名を載せないと、受け手は「表示中のタブ」を止めるしかなく、
    # 2台を同時に記録しているときに別の機器の記録を打ち切ってしまう。
    stop_requested = pyqtSignal(str)
    
    def __init__(self, device_name: str, file_path: str, parent=None,
                 size_provider=None):
        """
        Args:
            device_name: 記録している機器
            file_path: 保存先（表示に使うだけで、読みには行かない）
            parent: 親ウィジェット
            size_provider: 記録できたバイト数を返す関数（引数なし）。
                記録している側が数えた値を渡す。省略時は 0 を表示する
        """
        super().__init__(parent)
        self.device_name = device_name
        self.file_path = file_path
        self.size_provider = size_provider
        self.start_time = datetime.now()
        # 停止の要求を出したか（閉じるときに重ねて出さない）
        self._stop_sent = False
        
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

        # 記録先の詰まりなど、いまの状態（何も無ければ隠す。set_status）
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(
            theme.band_style(self, bold=True, font_size="9pt"))
        self.status_label.hide()
        layout.addWidget(self.status_label)
        # 行を消したあとの余った高さは、行のあった所に空けておく（set_status
        # は伸ばしたダイアログを縮めない。ほかの行の間が広がらないように）
        layout.addStretch()
        
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
        """記録できたバイト数を表示する（保存先は見に行かない）

        受信した文字数ではなくファイル上のバイト数を出す。テキストモードで
        開いているので改行は CRLF になり、日本語は UTF-8 で 3 バイトになる。
        数えるのは記録している側（size_provider）で、ここではファイル
        システムを触らない。以前は 1 秒ごとに os.path.getsize を呼んで
        いたが、共有フォルダ（SMB）へ記録していると、この 1 回が接続の
        都合で秒単位に延びて画面全体が止まる（実測: getsize が 3 秒
        かかると、受信が無くても 50ms の描画タイマーの最大間隔が
        3.06 秒。基準 0.067 秒）。
        """
        size = self.size_provider() if self.size_provider is not None else 0
        self.bytes_label.setText(f"記録したバイト数: {size:,} バイト")

    def set_status(self, text: str) -> None:
        """状態の行を text にする（空なら消して隠す）。

        記録先の詰まりの知らせに使う（TerminalWidget の見回りが呼ぶ）。
        ダイアログを前に出さず、活性化もしない。打っている最中にキーの
        行き先が移ると、残りの文字が捨てられ、Enter が機器へ届かない。
        """
        if text == self.status_label.text():
            return
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))
        # 折り返した行が切れないよう、足りなければ高さだけ伸ばす。窓は
        # 最小の高さ（1 行分）までしか自分では伸びず、2 行目から先が切れて
        # いた。幅と、利用者が広げた大きさは変えない。消しても縮めない
        # （止めては再開するたびに停止ボタンの位置が上下しないように）。
        # resize は大きさを変えるだけで、前に出さず活性化もしない
        need = self.heightForWidth(self.width())
        if need > self.height():
            self.resize(self.width(), need)

    def _on_stop(self):
        """停止ボタンクリック時の処理"""
        self._stop_sent = True
        self.timer.stop()
        self.stop_requested.emit(self.device_name)
        self.close()

    def keyPressEvent(self, event):
        """Esc では閉じない（記録中であることを示す表示はこれしか無い）

        QDialog の既定では Esc で隠れ、記録は続いたまま記録中だと分からなく
        なる。記録を止めずに隠す操作は受け付けない。
        """
        if event.matches(QKeySequence.StandardKey.Cancel):
            event.accept()
            return
        super().keyPressEvent(event)
    
    def closeEvent(self, event):
        """ダイアログを閉じる時の処理

        × や Alt+F4 で閉じたら「記録停止」と同じく記録を止める（見えている
        ときだけ記録中、を崩さない）。閉じる要求は断らない。断ると「今すぐ
        更新」の QApplication.closeAllWindows() がそこで止まり、主窓の
        closeEvent（記録の書き切り）を通らずアプリも終わらない。記録を
        止める後始末の経路から閉じたときも出るが、記録はもう外れている
        ので受け手は何もしない。
        """
        self.timer.stop()
        if not self._stop_sent:
            self._stop_sent = True
            self.stop_requested.emit(self.device_name)
        super().closeEvent(event)
