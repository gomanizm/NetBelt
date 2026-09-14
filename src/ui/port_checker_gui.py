#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ポートチェッカー GUI
TCP/UDPポートの状態を確認するGUIツール
"""

import sys
import socket
import subprocess
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLabel, QLineEdit, 
                             QTextEdit, QGroupBox, QSpinBox, QComboBox, QRadioButton,
                             QButtonGroup)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont

from core.sockets import set_exclusive_bind


def _local_port(local_address: str):
    """netstat のローカルアドレス列（例 0.0.0.0:135, [::]:135）からポート番号を取り出す。"""
    _, sep, port = local_address.rpartition(":")
    if not sep or not port.isdigit():
        return None
    return int(port)


def select_netstat_lines(output: str, port, protocol: str,
                         listening_only: bool = False) -> list:
    """netstat -ano の出力から、条件に合う行だけを返す。

    findstr :{port} のような部分文字列の判定は、80 で 8080 やリモート側の
    :80 まで拾い、別プロセスを原因として表示してしまう。列に分けて、
    ローカルアドレス列のポート番号を完全一致で比べる。

    Args:
        output: netstat -ano の出力全体
        port: ポート番号。None なら全ポート
        protocol: 'TCP' または 'UDP'（行頭のプロトコル列と比べる。IPv6 の
            行も同じ列名なので一緒に拾う）
        listening_only: True なら TCP の状態列が LISTENING の行だけ。
            UDP は状態列を持たないのでそのまま
    """
    selected = []
    for line in output.splitlines():
        parts = line.split()
        # 列: プロトコル, ローカル, 外部, [状態], PID（UDP は状態列が無い）
        if len(parts) < 4 or parts[0].upper() != protocol.upper():
            continue
        if port is not None and _local_port(parts[1]) != port:
            continue
        if listening_only and protocol.upper() == "TCP" and \
                (len(parts) < 5 or parts[3].upper() != "LISTENING"):
            continue
        selected.append(line)
    return selected


class PortCheckThread(QThread):
    """ポートチェックを別スレッドで実行"""
    result_ready = pyqtSignal(str)
    
    def __init__(self, port, check_type, protocol):
        super().__init__()
        self.port = port
        self.check_type = check_type
        self.protocol = protocol  # 'UDP' or 'TCP'
    
    def run(self):
        result = ""
        
        # ポートバインドテスト
        if self.check_type in ["all", "bind"]:
            result += "=" * 60 + "\n"
            result += f"【{self.protocol}ポートバインドテスト】\n"
            result += "=" * 60 + "\n"
            try:
                if self.protocol == "UDP":
                    test_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                else:  # TCP
                    test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                
                # SO_REUSEADDR を立ててはいけない。Windows では占有側も
                # SO_REUSEADDR を持っていると同じポートへの bind が通り、
                # 使用中なのに「バインド可能」と出る。排他バインドなら、
                # 占有側の設定や 127.0.0.1 固定の bind に関係なく 10048 で
                # 断られる
                set_exclusive_bind(test_socket)
                test_socket.bind(('', self.port))
                
                if self.protocol == "TCP":
                    test_socket.listen(1)
                
                test_socket.close()
                result += f"✓ ポート {self.port}/{self.protocol} はバインド可能です\n"
                result += f"  → 現在このポートは使用されていません\n"
                if self.protocol == "UDP":
                    result += f"  → SNMPTrapリスナーを起動できます\n\n"
                else:
                    result += f"  → サーバーアプリケーションを起動できます\n\n"
            except OSError as e:
                # 10048: Address already in use。10013 (errno 13): 占有側が
                # SO_REUSEADDR 無しで、こちらが SO_REUSEADDR 付きのときに
                # 出る「アクセス許可で禁じられた方法」。どちらも使用中
                if e.errno in (10048, 13) or getattr(e, "winerror", None) in (10048, 10013):
                    result += f"✗ ポート {self.port}/{self.protocol} は既に使用されています\n"
                    result += f"  → 別のプログラムがこのポートを使用中です\n"
                    result += f"  → 下記のプロセス情報を確認してください\n\n"
                else:
                    result += f"✗ エラー: {e}\n\n"
        
        # netstat コマンドでポート使用状況を確認
        if self.check_type in ["all", "netstat"]:
            result += "=" * 60 + "\n"
            result += "【ポート使用状況（netstat）】\n"
            result += "=" * 60 + "\n"
            # Windows netstat コマンド。findstr で絞らず、行を列に分けて
            # ローカル側のポート番号とプロトコルを完全一致で選ぶ
            # 取得に失敗したときは output = "" にしない。0 件（＝本当に空き）と
            # 区別が付かなくなり、確認できていないポートを「使用されていません」
            # と言い切ってしまうため
            try:
                output = subprocess.check_output("netstat -ano", shell=True, text=True, 
                                               stderr=subprocess.STDOUT)
            except (subprocess.CalledProcessError, OSError) as e:
                error, lines = e, []
            else:
                error, lines = None, select_netstat_lines(output, self.port, self.protocol)
            if error is not None:
                result += f"{self.protocol}接続情報の取得に失敗しました: {error}\n"
                result += "→ このポートの使用状況は確認できていません\n\n"
            elif not lines:
                result += f"ポート {self.port}/{self.protocol} を使用している接続は見つかりませんでした\n"
                result += "→ このポートは現在使用されていません\n\n"
            else:
                result += f"ポート {self.port}/{self.protocol} を使用している接続:\n\n"
                result += "\n".join(lines) + "\n\n"
                
                # PIDを抽出してプロセス名を取得
                pids = set()
                for line in lines:
                    pid = line.split()[-1]
                    if pid.isdigit():
                        pids.add(pid)
                
                if pids:
                    result += "\n関連プロセス情報:\n"
                    result += "-" * 60 + "\n"
                    for pid in pids:
                        try:
                            cmd = f"tasklist /FI \"PID eq {pid}\" /FO CSV /NH"
                            proc_info = subprocess.check_output(cmd, shell=True, 
                                                               text=True, 
                                                               stderr=subprocess.STDOUT)
                            # CSVフォーマットをパース
                            proc_info = proc_info.strip().replace('"', '')
                            parts = proc_info.split(',')
                            if len(parts) >= 2:
                                proc_name = parts[0]
                                result += f"  PID {pid}: {proc_name}\n"
                        except subprocess.CalledProcessError:
                            result += f"  PID {pid}: プロセス情報取得失敗\n"
                    result += "\n"
        
        # ポートをリスニングしているプロセスを表示
        if self.check_type in ["all", "listening"]:
            result += "=" * 60 + "\n"
            result += f"【{self.protocol}リスニングポート一覧】\n"
            result += "=" * 60 + "\n"
            try:
                # -p TCP は IPv4 だけなので、全体を取ってプロトコル列で選ぶ
                cmd = "netstat -ano"
                output = subprocess.check_output(cmd, shell=True, text=True,
                                               stderr=subprocess.STDOUT)
                
                # 指定ポートを待ち受けている行を探す。TCP は状態列が
                # LISTENING のものだけ（ESTABLISHED だけの対は待ち受けではない）
                relevant_lines = select_netstat_lines(
                    output, self.port, self.protocol,
                    listening_only=(self.protocol == "TCP"))
                
                if relevant_lines:
                    result += f"{self.protocol}ポート {self.port} を使用している接続:\n\n"
                    for line in relevant_lines[:20]:  # 最大20行まで表示
                        result += line + "\n"
                else:
                    # すべてのポートを表示（最大20行）
                    result += f"{self.protocol}接続の一覧（最大20件）:\n\n"
                    for line in select_netstat_lines(output, None, self.protocol)[:20]:
                        result += line + "\n"
                result += "\n"
            except subprocess.CalledProcessError:
                result += f"{self.protocol}接続情報の取得に失敗しました\n\n"
        
        # ファイアウォール情報
        if self.check_type in ["all", "firewall"]:
            result += "=" * 60 + "\n"
            result += "【Windowsファイアウォール確認方法】\n"
            result += "=" * 60 + "\n"
            result += "以下のコマンドを管理者権限で実行してファイアウォールを確認:\n\n"
            result += f"  netsh advfirewall firewall show rule name=all | findstr {self.port}\n\n"
            result += "または、以下の手順で確認:\n"
            result += "1. Windowsファイアウォールを開く\n"
            result += "2. 「詳細設定」をクリック\n"
            result += "3. 「受信の規則」を確認\n"
            result += f"4. ポート {self.port}/{self.protocol} が許可されているか確認\n\n"
        
        # プロトコル固有の情報
        if self.check_type == "all" and self.port == 162 and self.protocol == "UDP":
            result += "=" * 60 + "\n"
            result += "【SNMPTrap 受信のトラブルシューティング】\n"
            result += "=" * 60 + "\n"
            result += "SNMPTrapが受信できない場合の確認事項:\n\n"
            result += "1. ポートがバインド可能か（上記のバインドテスト結果を確認）\n"
            result += "2. 別のSNMPサービスが既に起動していないか\n"
            result += "3. Windowsファイアウォールでポート162/UDPが許可されているか\n"
            result += "4. アンチウイルスソフトがポートをブロックしていないか\n"
            result += "5. SNMPTrapの送信元IPアドレスが正しいか\n"
            result += "6. リスナーが正しいIPアドレス（0.0.0.0または特定IP）でバインドしているか\n\n"
            result += "推奨事項:\n"
            result += "• 管理者権限でアプリケーションを起動してみる\n"
            result += "• 一時的にファイアウォールを無効にしてテストする\n"
            result += "• Wiresharkなどでパケットが到達しているか確認する\n\n"
        
        # 一般的なポート情報
        if self.check_type == "all":
            result += "=" * 60 + "\n"
            result += "【一般的なポート情報】\n"
            result += "=" * 60 + "\n"
            common_ports = {
                "TCP": {
                    20: "FTP Data",
                    21: "FTP Control",
                    22: "SSH",
                    23: "Telnet",
                    25: "SMTP",
                    53: "DNS",
                    80: "HTTP",
                    110: "POP3",
                    143: "IMAP",
                    443: "HTTPS",
                    3306: "MySQL",
                    3389: "RDP",
                    5432: "PostgreSQL",
                    8080: "HTTP Alternate"
                },
                "UDP": {
                    53: "DNS",
                    67: "DHCP Server",
                    68: "DHCP Client",
                    69: "TFTP",
                    123: "NTP",
                    161: "SNMP",
                    162: "SNMP Trap",
                    514: "Syslog"
                }
            }
            
            if self.port in common_ports[self.protocol]:
                result += f"ポート {self.port}/{self.protocol} は一般的に以下の用途で使用されます:\n"
                result += f"  → {common_ports[self.protocol][self.port]}\n\n"
            else:
                result += f"ポート {self.port}/{self.protocol} の情報:\n"
                result += f"  カスタムポートまたは非標準ポートです\n\n"
        
        self.result_ready.emit(result)


class PortCheckerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.init_ui()
        
    def init_ui(self):
        self.setWindowTitle("ポートチェッカー")
        self.setGeometry(100, 100, 850, 750)
        
        # メインウィジェット
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout()
        main_widget.setLayout(layout)
        
        # タイトル
        title = QLabel("TCP/UDP ポートチェッカー")
        title_font = QFont()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        
        # 説明
        desc = QLabel("TCP/UDPポートの状態を確認します")
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(desc)
        
        # ポート設定グループ
        port_group = QGroupBox("ポート設定")
        port_layout = QVBoxLayout()
        port_group.setLayout(port_layout)
        
        # ポート番号設定
        port_row = QHBoxLayout()
        port_label = QLabel("チェックするポート:")
        port_row.addWidget(port_label)
        
        self.port_spinbox = QSpinBox()
        self.port_spinbox.setRange(1, 65535)
        self.port_spinbox.setValue(162)
        self.port_spinbox.setMinimumWidth(100)
        port_row.addWidget(self.port_spinbox)
        
        port_row.addStretch()
        port_layout.addLayout(port_row)
        
        # プロトコル選択
        protocol_row = QHBoxLayout()
        protocol_label = QLabel("プロトコル:")
        protocol_row.addWidget(protocol_label)
        
        self.protocol_group = QButtonGroup()
        self.udp_radio = QRadioButton("UDP")
        self.tcp_radio = QRadioButton("TCP")
        self.udp_radio.setChecked(True)
        
        self.protocol_group.addButton(self.udp_radio)
        self.protocol_group.addButton(self.tcp_radio)
        
        protocol_row.addWidget(self.udp_radio)
        protocol_row.addWidget(self.tcp_radio)
        protocol_row.addStretch()
        port_layout.addLayout(protocol_row)
        
        # 一般的なポートのプリセット
        preset_row = QHBoxLayout()
        preset_label = QLabel("プリセット:")
        preset_row.addWidget(preset_label)
        
        presets = [
            ("SNMP Trap (162/UDP)", 162, "UDP"),
            ("HTTP (80/TCP)", 80, "TCP"),
            ("HTTPS (443/TCP)", 443, "TCP"),
            ("SSH (22/TCP)", 22, "TCP"),
            ("Telnet (23/TCP)", 23, "TCP"),
            ("Syslog (514/UDP)", 514, "UDP"),
        ]
        
        for preset_name, port, protocol in presets:
            btn = QPushButton(preset_name)
            btn.clicked.connect(lambda checked, p=port, pr=protocol: self.set_preset(p, pr))
            preset_row.addWidget(btn)
        
        preset_row.addStretch()
        port_layout.addLayout(preset_row)
        
        layout.addWidget(port_group)
        
        # ボタングループ
        button_group = QGroupBox("チェック実行")
        button_layout = QVBoxLayout()
        button_group.setLayout(button_layout)
        
        # ボタン行1
        button_row1 = QHBoxLayout()
        self.check_all_btn = QPushButton("完全チェック")
        self.check_all_btn.clicked.connect(lambda: self.run_check("all"))
        button_row1.addWidget(self.check_all_btn)
        
        self.check_bind_btn = QPushButton("バインドテスト")
        self.check_bind_btn.clicked.connect(lambda: self.run_check("bind"))
        button_row1.addWidget(self.check_bind_btn)
        
        button_layout.addLayout(button_row1)
        
        # ボタン行2
        button_row2 = QHBoxLayout()
        self.check_netstat_btn = QPushButton("使用状況確認")
        self.check_netstat_btn.clicked.connect(lambda: self.run_check("netstat"))
        button_row2.addWidget(self.check_netstat_btn)
        
        self.check_listening_btn = QPushButton("UDP一覧")
        self.check_listening_btn.clicked.connect(lambda: self.run_check("listening"))
        button_row2.addWidget(self.check_listening_btn)
        
        button_layout.addLayout(button_row2)
        
        # クリアボタン
        self.clear_btn = QPushButton("結果をクリア")
        self.clear_btn.clicked.connect(self.clear_result)
        button_layout.addWidget(self.clear_btn)
        
        layout.addWidget(button_group)
        
        # 結果表示エリア
        result_group = QGroupBox("チェック結果")
        result_layout = QVBoxLayout()
        result_group.setLayout(result_layout)
        
        self.result_text = QTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setFont(QFont("Courier New", 9))
        result_layout.addWidget(self.result_text)
        
        layout.addWidget(result_group)
        
        # ステータスバー
        self.statusBar().showMessage("準備完了")
        
    def set_preset(self, port, protocol):
        """プリセットを適用"""
        self.port_spinbox.setValue(port)
        if protocol == "UDP":
            self.udp_radio.setChecked(True)
        else:
            self.tcp_radio.setChecked(True)
    
    def run_check(self, check_type):
        """ポートチェックを実行"""
        port = self.port_spinbox.value()
        protocol = "UDP" if self.udp_radio.isChecked() else "TCP"
        
        # ボタンを無効化
        self.set_buttons_enabled(False)
        self.statusBar().showMessage(f"ポート {port}/{protocol} をチェック中...")
        
        # 前回の結果をクリア
        self.result_text.clear()
        
        # チェック実行
        self.check_thread = PortCheckThread(port, check_type, protocol)
        self.check_thread.result_ready.connect(self.display_result)
        self.check_thread.finished.connect(self.check_finished)
        self.check_thread.start()
    
    def display_result(self, result):
        """結果を表示"""
        self.result_text.setPlainText(result)
        
    def check_finished(self):
        """チェック完了"""
        self.set_buttons_enabled(True)
        self.statusBar().showMessage("チェック完了")
    
    def set_buttons_enabled(self, enabled):
        """ボタンの有効/無効を設定"""
        self.check_all_btn.setEnabled(enabled)
        self.check_bind_btn.setEnabled(enabled)
        self.check_netstat_btn.setEnabled(enabled)
        self.check_listening_btn.setEnabled(enabled)
    
    def clear_result(self):
        """結果をクリア"""
        self.result_text.clear()
        self.statusBar().showMessage("結果をクリアしました")


def main():
    app = QApplication(sys.argv)
    
    # スタイル設定
    app.setStyle("Fusion")
    
    window = PortCheckerGUI()
    window.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()