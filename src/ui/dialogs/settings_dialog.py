"""設定ダイアログ

config.json の settings 配下のうち、これまで GUI から編集できなかった項目を扱う。
保存はこのダイアログ自身が行う（MacroDialog / PresetEditDialog と同じ流儀）。
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget, QWidget,
    QPushButton, QSpinBox, QFontComboBox, QColorDialog, QLabel, QTextEdit,
    QMessageBox, QCheckBox
)
from PyQt6.QtGui import QColor, QFont

from ..terminal_widget import TerminalWidget


class SettingsDialog(QDialog):
    """アプリケーション設定ダイアログ"""

    def __init__(self, parent=None, config_manager=None):
        """
        初期化

        Args:
            parent: 親ウィジェット
            config_manager: 設定マネージャ（呼び出し側の共有インスタンスを渡すこと。
                ここで新規生成すると、終了時の _save_layout で書き戻されて設定が消える）
        """
        super().__init__(parent)
        self.config_manager = config_manager

        self.setWindowTitle("設定")
        self.setModal(True)
        self.resize(520, 460)

        self._create_ui()
        self._load_data()

    def _create_ui(self):
        """UI作成"""
        layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._create_terminal_tab(), "ターミナル")
        self.tabs.addTab(self._create_update_tab(), "更新")
        layout.addWidget(self.tabs)

        button_layout = QHBoxLayout()
        button_layout.addStretch()
        self.ok_btn = QPushButton("OK")
        self.ok_btn.clicked.connect(self._on_ok)
        self.cancel_btn = QPushButton("キャンセル")
        self.cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(self.ok_btn)
        button_layout.addWidget(self.cancel_btn)
        layout.addLayout(button_layout)

    def _create_terminal_tab(self) -> QWidget:
        """ターミナルタブを作る"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        form = QFormLayout()

        self.bg_color_button = QPushButton()
        self.bg_color_button.clicked.connect(lambda: self._pick_color("background"))
        form.addRow("背景色:", self.bg_color_button)

        self.text_color_button = QPushButton()
        self.text_color_button.clicked.connect(lambda: self._pick_color("text"))
        form.addRow("文字色:", self.text_color_button)

        self.font_combo = QFontComboBox()
        self.font_combo.setFontFilters(QFontComboBox.FontFilter.MonospacedFonts)
        self.font_combo.currentFontChanged.connect(lambda _: self._update_preview())
        form.addRow("フォント:", self.font_combo)

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(TerminalWidget.FONT_SIZE_MIN,
                                     TerminalWidget.FONT_SIZE_MAX)
        self.font_size_spin.setSuffix(" pt")
        self.font_size_spin.valueChanged.connect(lambda _: self._update_preview())
        form.addRow("サイズ:", self.font_size_spin)

        layout.addLayout(form)

        layout.addWidget(QLabel("プレビュー:"))
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(90)
        self.preview.setPlainText(
            "Router#show ip interface brief\n"
            "Interface     IP-Address    OK? Status\n"
            "GigabitEthernet0/0  192.0.2.1   YES up")
        layout.addWidget(self.preview)

        return widget

    def _pick_color(self, target: str):
        """
        色を選ぶ

        Args:
            target: 'background' または 'text'
        """
        current = self._background_color if target == "background" else self._text_color
        color = QColorDialog.getColor(QColor(current), self, "色を選択")
        if not color.isValid():
            return
        if target == "background":
            self._background_color = color.name().upper()
        else:
            self._text_color = color.name().upper()
        self._update_color_buttons()
        self._update_preview()

    def _update_color_buttons(self):
        """色ボタンに現在の色を見本として表示する"""
        self.bg_color_button.setText(self._background_color)
        self.bg_color_button.setStyleSheet(
            f"background-color: {self._background_color}; color: {self._text_color};")
        self.text_color_button.setText(self._text_color)
        self.text_color_button.setStyleSheet(
            f"background-color: {self._background_color}; color: {self._text_color};")

    def _update_preview(self):
        """プレビュー欄へ現在の選択を反映する"""
        font = QFont(self.font_combo.currentFont().family(), self.font_size_spin.value())
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.preview.setFont(font)
        self.preview.setStyleSheet(
            f"background-color: {self._background_color}; color: {self._text_color};")

    def _create_update_tab(self) -> QWidget:
        """更新タブを作る"""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        self.check_on_startup_box = QCheckBox("起動時に更新を確認する")
        layout.addWidget(self.check_on_startup_box)

        layout.addWidget(QLabel("スキップ中のバージョン:"))
        skip_layout = QHBoxLayout()
        self.skipped_version_label = QLabel()
        skip_layout.addWidget(self.skipped_version_label)
        skip_layout.addStretch()
        self.clear_skip_button = QPushButton("スキップを解除")
        self.clear_skip_button.clicked.connect(self._on_clear_skip)
        skip_layout.addWidget(self.clear_skip_button)
        layout.addLayout(skip_layout)

        # GitHub のトークンはここに出さない。config.json には平文で残るうえ、
        # 環境変数 GITHUB_TOKEN が優先される仕組みがあるため。
        layout.addStretch()
        return widget

    def _on_clear_skip(self):
        """スキップ中のバージョンを解除する（保存は save_settings で行う）"""
        self._skip_cleared = True
        self._update_skip_display(None)

    def _update_skip_display(self, version):
        """
        スキップ表示を更新する

        Args:
            version: スキップ中のバージョン文字列。無ければ None
        """
        if version:
            self.skipped_version_label.setText(f"v{version}")
            self.clear_skip_button.setEnabled(True)
        else:
            self.skipped_version_label.setText("ありません")
            self.clear_skip_button.setEnabled(False)

    def _load_data(self):
        """config から現在値を読み込む"""
        raw = self.config_manager.get_server_settings("terminal") if self.config_manager else {}
        # 生値ではなく、ターミナルが実際に使う正規化済みの値を表示する。
        # 生値のままだと "not-a-color" や 1000 がそのまま画面に出て、
        # 何も変えずに OK を押しただけで壊れた値を書き戻してしまう。
        terminal = TerminalWidget.normalize_terminal_settings(raw)

        self._background_color = terminal["background_color"]
        self._text_color = terminal["text_color"]
        self.font_combo.setCurrentFont(QFont(terminal["font_family"]))
        self.font_size_spin.setValue(terminal["font_size"])

        self._update_color_buttons()
        self._update_preview()

        self._skip_cleared = False
        if self.config_manager:
            self.check_on_startup_box.setChecked(self.config_manager.get_check_on_startup())
            self._update_skip_display(self.config_manager.get_skipped_version())
        else:
            self._update_skip_display(None)

    def save_settings(self) -> bool:
        """
        入力内容を config へ保存する

        settings 全体を置換する update_settings ではなく、浅いマージの
        set_server_settings を使う（ui_layout など他のセクションを消さないため）。

        Returns:
            保存成功時True
        """
        if not self.config_manager:
            return False

        saved = self.config_manager.set_server_settings("terminal", {
            "background_color": self._background_color,
            "text_color": self._text_color,
            "font_family": self.font_combo.currentFont().family(),
            "font_size": self.font_size_spin.value(),
        })

        # 更新設定の setter はそれぞれ独立に save_config() を呼ぶ。どれか一つでも
        # 失敗したら保存失敗として扱う。ターミナル設定の結果だけ返すと、
        # 更新設定が書けていないのに成功したように見える。
        if not self.config_manager.set_check_on_startup(
                self.check_on_startup_box.isChecked()):
            saved = False
        if self._skip_cleared and not self.config_manager.set_skipped_version(None):
            saved = False

        return saved

    def _on_ok(self):
        """OKボタン押下時の処理"""
        if self.save_settings():
            self.accept()
            return
        # 黙って閉じないままだと、OK が効かない理由が利用者に伝わらない
        QMessageBox.warning(
            self, "エラー",
            "設定を保存できませんでした。\n"
            "設定ファイルに書き込めない可能性があります。")
