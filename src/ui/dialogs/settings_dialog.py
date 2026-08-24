"""設定ダイアログ

config.json の settings 配下のうち、これまで GUI から編集できなかった項目を扱う。
保存はこのダイアログ自身が行う（MacroDialog / PresetEditDialog と同じ流儀）。
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget, QWidget,
    QPushButton, QSpinBox, QFontComboBox, QColorDialog, QLabel, QTextEdit
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

    def _load_data(self):
        """config から現在値を読み込む"""
        defaults = TerminalWidget.DEFAULT_TERMINAL_SETTINGS
        terminal = self.config_manager.get_server_settings("terminal") if self.config_manager else {}

        self._background_color = terminal.get("background_color") or defaults["background_color"]
        self._text_color = terminal.get("text_color") or defaults["text_color"]
        self.font_combo.setCurrentFont(
            QFont(terminal.get("font_family") or defaults["font_family"]))
        self.font_size_spin.setValue(terminal.get("font_size") or defaults["font_size"])

        self._update_color_buttons()
        self._update_preview()

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

        return self.config_manager.set_server_settings("terminal", {
            "background_color": self._background_color,
            "text_color": self._text_color,
            "font_family": self.font_combo.currentFont().family(),
            "font_size": self.font_size_spin.value(),
        })

    def _on_ok(self):
        """OKボタン押下時の処理"""
        if self.save_settings():
            self.accept()
