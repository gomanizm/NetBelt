from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QPoint, QRect, QSize
from PyQt6.QtGui import QKeySequence
from datetime import datetime
from ui import theme


class LogRecordingDialog(QDialog):
    """ログ記録中に表示するダイアログ"""
    
    # 停止要求シグナル（どの機器のダイアログかを必ず伝える）。
    # 機器名を載せないと、受け手は「表示中のタブ」を止めるしかなく、
    # 2台を同時に記録しているときに別の機器の記録を打ち切ってしまう。
    stop_requested = pyqtSignal(str)
    
    # 状態の行に出す知らせ（どれを出すかは TerminalWidget の見回りが決める）
    STATUS_HELD = ("記録先への書き込みが遅れているため、受信を止めています"
                   "（画面にも記録にも欠けは出ません。記録先が応答すると"
                   "再開します）。止めている間も、打った文字は機器へ送られ"
                   "ます（エコーは再開してから表示されます）。")
    STATUS_LAGGING = ("記録先への書き込みが遅れています。記録する分をメモリに"
                      "溜めています（保存先の接続を確認してください）。")
    # 並べるときのダイアログどうしの隙間（px）
    PLACE_GAP = 8

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

    def place_apart(self, others) -> None:
        """表示する前に、開いているほかの記録中ダイアログ（others）と重ならない所へ置く。

        置かないと、どれも親の中央に出て、後から始めた記録のダイアログが先の
        ダイアログにぴったり重なり、先の記録の状態の行が隠れていた（止まった
        理由が見えず、エコーの見えないまま打ち直すと機器へ二重に送る）。
        状態の行が出て伸びる分（set_status）も空けておく。親の中央から近い
        順に、同じ段の左右、次に上下の段を（画面の内側へ寄せて）探し、どれとも
        重ならない最初の所へ置く。画面に空きが無ければ、重なる面積が最も
        小さい所へ置く（既定の位置のままだと先のダイアログに丸ごと重なる）。
        move は位置を決めるだけで、前に出さず活性化もしない。
        """
        # show() と同じ大きさにする（show() と同じく、自動で決めた大きさの扱い）
        self.adjustSize()
        self.setAttribute(Qt.WidgetAttribute.WA_Resized, False)
        room = self._status_room()
        self._full_height = self.height() + room
        shown = [d for d in others if d is not self and d.isVisible()]
        if not shown:
            return
        taken = [d.frameGeometry().adjusted(
            0, 0, 0, max(0, getattr(d, "_full_height", 0) - d.height()))
            for d in shown]
        # 枠（タイトルバーなど）の厚みは、表示しているダイアログから借りる
        frame = shown[0].frameGeometry()
        size = QSize(self.width() + frame.width() - shown[0].width(),
                     self.height() + frame.height() - shown[0].height() + room)
        host = self.parentWidget().window() if self.parentWidget() else self
        screen = host.screen()
        if screen is None:          # 画面が 1 つも無い間（ドックの抜き差しなど）
            return
        area = screen.availableGeometry()
        center = host.mapToGlobal(QPoint(host.width() // 2, host.height() // 2))
        # 既定の位置（QDialog が親の中央に置く所）と同じ見積もりにして、最初の
        # ダイアログと段を揃える（Windows では枠の左の厚みが 0 で、10 と 40）
        fx = max(d.geometry().x() - d.x() for d in shown)
        fy = max(d.geometry().y() - d.y() for d in shown)
        if not fx or not fy or fx >= 10 or fy >= 40:
            fx, fy = 10, 40
        base = center - QPoint(self.width() // 2 + fx, self.height() // 2 + fy)
        step_x = size.width() + self.PLACE_GAP
        step_y = size.height() + self.PLACE_GAP

        def spread(count):
            yield 0
            for i in range(1, count + 1):
                yield i
                yield -i
        best = None
        for dy in spread(area.height() // step_y + 1):
            for dx in spread(area.width() // step_x + 1):
                pos = base + QPoint(dx * step_x, dy * step_y)
                pos = QPoint(   # 画面からはみ出す所は内側へ寄せる
                    max(area.left(), min(pos.x(), area.right() + 1 - size.width())),
                    max(area.top(), min(pos.y(), area.bottom() + 1 - size.height())))
                covered = sum(r.width() * r.height() for r in (
                    QRect(pos, size).intersected(t) for t in taken))
                if best is None or covered < best[0]:
                    best = (covered, pos)
                if not covered:
                    break
            if not best[0]:
                break
        self.move(best[1])

    def _status_room(self) -> int:
        """状態の行に知らせを出すと、いまの幅で高さがどれだけ伸びるか（表示する前に測る）"""
        label = self.status_label
        if label.isVisibleTo(self):
            return 0
        now = self.heightForWidth(self.width())
        need = now
        label.show()                # 窓を表示する前なので画面には出ない
        for text in (self.STATUS_HELD, self.STATUS_LAGGING):
            label.setText(text)
            need = max(need, self.heightForWidth(self.width()))
        label.hide()
        label.setText("")
        return max(0, need - now)

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
