from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QPlainTextEdit, QPushButton)


class PasteConfirmDialog(QDialog):
    """改行を含む貼り付けを、送る前に見せて確かめるダイアログ

    機器は改行ごとにコマンドとして実行するので、誤って貼ると取り消せない。
    Enter の押し癖で送らないよう、既定のボタンはキャンセルにする。
    """

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("貼り付けの確認")
        self.setModal(True)
        self.setMinimumSize(480, 320)

        shown = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = len(shown.splitlines())

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"次の {lines} 行を機器へ送ります。改行ごとにコマンドとして実行されます。"))

        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(shown)
        layout.addWidget(view)

        buttons = QHBoxLayout()
        buttons.addStretch()
        send = QPushButton("送信")
        send.setAutoDefault(False)
        send.clicked.connect(self.accept)
        buttons.addWidget(send)
        cancel = QPushButton("キャンセル")
        cancel.setDefault(True)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        cancel.setFocus()
