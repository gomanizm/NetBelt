"""
マクロ管理モジュール

キープアライブ機能とコマンドリスト実行機能を提供します。
"""

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from typing import Dict, List, Optional, Callable
import time


class MacroManager(QObject):
    """マクロ機能を管理するクラス"""
    
    # シグナル
    command_output = pyqtSignal(str, str)  # (device_name, output_text)
    macro_finished = pyqtSignal(str)  # (device_name)
    macro_error = pyqtSignal(str, str)  # (device_name, error_message)
    # コマンドリストの開始・終了（停止・完了・エラーを含む）: (device_name, 実行中か)
    command_list_state_changed = pyqtSignal(str, bool)
    
    def __init__(self):
        super().__init__()
        
        # キープアライブタイマー管理（device_name -> QTimer）
        self._keepalive_timers: Dict[str, QTimer] = {}
        
        # コマンドリスト実行管理（device_name -> コマンドリスト）
        self._command_lists: Dict[str, List[str]] = {}
        self._command_indices: Dict[str, int] = {}
        self._command_delays: Dict[str, int] = {}  # ミリ秒単位
        self._command_timers: Dict[str, QTimer] = {}
        
        # コマンド送信コールバック（device_name -> callback）
        self._send_callbacks: Dict[str, Callable] = {}
        # コマンドリスト専用の送信コールバック（device_name -> callback）
        self._command_send_callbacks: Dict[str, Callable] = {}
        # 送信待ちのコマンドを取り消すコールバック（device_name -> callback）
        self._cancel_callbacks: Dict[str, Callable] = {}

    def register_send_callback(self, device_name: str, callback: Callable,
                               command_callback: Optional[Callable] = None,
                               cancel_callback: Optional[Callable] = None):
        """
        コマンド送信コールバックを登録

        Args:
            device_name: 機器名
            callback: コマンド送信関数（引数: command文字列）
            command_callback: コマンドリスト専用の送信関数。省略時は callback を使う。
                送信先が列に溜める作りなら、cancel_callback で取り消せるように
                印を付けて積む版を渡す。引数は (command文字列, on_sent) で、
                その行を実際に送り出したときに on_sent() を呼ぶこと（次の行
                までの遅延はそこから数える）。キープアライブの CR もこれで
                (CR, None, "keepalive") として積む
            cancel_callback: 送信待ちのコマンドを取り消す関数。引数なしで
                コマンドリストの、"keepalive" でキープアライブのぶんを取り消す。
                停止したのに、列に残ったコマンドが後から機器へ届くのを防ぐ
        """
        self._send_callbacks[device_name] = callback
        if command_callback is not None:
            self._command_send_callbacks[device_name] = command_callback
        else:
            self._command_send_callbacks.pop(device_name, None)
        if cancel_callback is not None:
            self._cancel_callbacks[device_name] = cancel_callback
        else:
            self._cancel_callbacks.pop(device_name, None)

    def unregister_send_callback(self, device_name: str):
        """
        コマンド送信コールバックを解除

        Args:
            device_name: 機器名
        """
        if device_name in self._send_callbacks:
            del self._send_callbacks[device_name]
        self._command_send_callbacks.pop(device_name, None)
        self._cancel_callbacks.pop(device_name, None)
    
    def start_keepalive(self, device_name: str, interval_seconds: int = 60):
        """
        キープアライブを開始
        
        Args:
            device_name: 機器名
            interval_seconds: 送信間隔（秒）
        """
        # 既にキープアライブが動作している場合は停止
        self.stop_keepalive(device_name)
        
        # タイマーを作成
        timer = QTimer()
        timer.timeout.connect(lambda: self._send_keepalive(device_name))
        timer.start(interval_seconds * 1000)  # ミリ秒単位
        
        self._keepalive_timers[device_name] = timer
    
    def stop_keepalive(self, device_name: str):
        """
        キープアライブを停止
        
        発火した CR が、長い貼り付けの排出待ちなどでまだ送られずに列に
        残っていることがある。停止したのに後から届き、改行なしで貼った行を
        実行してしまうので、ここで取り消す。

        Args:
            device_name: 機器名
        """
        if device_name in self._keepalive_timers:
            self._keepalive_timers[device_name].stop()
            del self._keepalive_timers[device_name]
            if device_name in self._cancel_callbacks:
                self._cancel_callbacks[device_name]("keepalive")
    
    def is_keepalive_active(self, device_name: str) -> bool:
        """
        キープアライブが動作中かチェック
        
        Args:
            device_name: 機器名
        
        Returns:
            bool: 動作中の場合True
        """
        return device_name in self._keepalive_timers
    
    def _send_keepalive(self, device_name: str):
        """
        キープアライブ用のエンターを送信
        
        Args:
            device_name: 機器名
        """
        if device_name in self._command_send_callbacks:
            # 空のエンターを、停止で取り消せるように印を付けて積む
            self._command_send_callbacks[device_name]("\r", None, "keepalive")
        elif device_name in self._send_callbacks:
            # 空のエンターを送信
            self._send_callbacks[device_name]("\r")
    
    def start_command_list(
        self, 
        device_name: str, 
        commands: List[str], 
        delay_ms: int = 1000
    ):
        """
        コマンドリスト実行を開始
        
        Args:
            device_name: 機器名
            commands: 実行するコマンドのリスト
            delay_ms: コマンド間の遅延（ミリ秒）
        """
        # 既にコマンドリストが実行中の場合は停止
        self.stop_command_list(device_name)
        
        # コマンドリストを保存
        self._command_lists[device_name] = commands
        self._command_indices[device_name] = 0
        self._command_delays[device_name] = delay_ms
        
        # タイマーを作成。1 回ずつ鳴らし、次の遅延は送った行を実際に送り
        # 出した時点から数え直す（_on_command_sent）
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._execute_next_command(device_name))
        self._command_timers[device_name] = timer
        self.command_list_state_changed.emit(device_name, True)
        
        # 最初のコマンドを実行
        self._execute_next_command(device_name)
    
    def stop_command_list(self, device_name: str):
        """
        コマンドリスト実行を停止

        送信先が列に溜める作りだと、停止した時点でまだ送っていないコマンドが
        残っていることがある（長い貼り付けの排出待ちなど）。そのまま放って
        おくと、停止したのに後から機器へ届くので、ここで取り消す。

        Args:
            device_name: 機器名
        """
        self._teardown_command_list(device_name, cancel_pending=True)

    def _teardown_command_list(self, device_name: str, cancel_pending: bool):
        """
        コマンドリストの実行状態を畳む

        Args:
            device_name: 機器名
            cancel_pending: 送信待ちのコマンドも取り消すか。走り切った場合は
                全行を送り出し済みなので取り消さない
        """
        was_active = device_name in self._command_timers
        if was_active:
            self._command_timers[device_name].stop()
            del self._command_timers[device_name]
        
        if device_name in self._command_lists:
            del self._command_lists[device_name]
            del self._command_indices[device_name]
            del self._command_delays[device_name]

        if was_active:
            if cancel_pending and device_name in self._cancel_callbacks:
                self._cancel_callbacks[device_name]()
            self.command_list_state_changed.emit(device_name, False)
    
    def is_command_list_active(self, device_name: str) -> bool:
        """
        コマンドリスト実行中かチェック
        
        Args:
            device_name: 機器名
        
        Returns:
            bool: 実行中の場合True
        """
        return device_name in self._command_timers
    
    def _execute_next_command(self, device_name: str):
        """
        次のコマンドを実行
        
        Args:
            device_name: 機器名
        """
        if device_name not in self._command_lists:
            return
        
        commands = self._command_lists[device_name]
        index = self._command_indices[device_name]
        
        # すべてのコマンドを実行した場合（最後の行は送り出し済み）
        if index >= len(commands):
            self.macro_finished.emit(device_name)
            self._teardown_command_list(device_name, cancel_pending=False)
            return

        # コマンドを送信
        command = commands[index]
        if device_name in self._send_callbacks:
            # インデックスは送る前に進める。送り出しの知らせ（on_sent）は
            # 送信の中から同期で届くことがある。送信が同期で失敗すると、
            # この中で切断の後始末（cleanup_device）まで走り、この機器の
            # 実行はすでに畳まれている（後で進めるとインデックスだけが
            # 作り直されて残る）
            self._command_indices[device_name] = index + 1
            timer = self._command_timers[device_name]

            def on_sent():
                self._on_command_sent(device_name, timer)

            # コマンド送信（改行付き）
            if device_name in self._command_send_callbacks:
                self._command_send_callbacks[device_name](command + "\r", on_sent)
            else:
                # 送り出しを知らせない送信先は、渡した時点で送り出したとみなす
                self._send_callbacks[device_name](command + "\r")
                on_sent()
        else:
            # コールバックが登録されていない場合はエラー
            self.macro_error.emit(
                device_name,
                "コマンド送信コールバックが登録されていません"
            )
            self.stop_command_list(device_name)

    def _on_command_sent(self, device_name: str, timer: QTimer):
        """送った行が機器へ送り出された。ここから次の行までの遅延を数える

        列に積んだ時点で数えると、長い貼り付けの後ろで待つ間に遅延が過ぎ、
        送り出すときには行の間の待ちが消えていた。全行を積み終えると完了
        扱いになり、その後の停止では残った行を取り消せなかった。
        """
        if self._command_timers.get(device_name) is not timer:
            return   # 停止した（やり直した）実行の行
        timer.start(self._command_delays[device_name])
    
    def cleanup_device(self, device_name: str):
        """
        機器切断時のクリーンアップ
        
        Args:
            device_name: 機器名
        """
        self.stop_keepalive(device_name)
        self.stop_command_list(device_name)
        self.unregister_send_callback(device_name)


class MacroPreset:
    """マクロプリセット（保存用）"""
    
    def __init__(self, name: str, commands: List[str], description: str = ""):
        self.name = name
        self.commands = commands
        self.description = description
    
    def to_dict(self) -> dict:
        """辞書形式に変換"""
        return {
            "name": self.name,
            "commands": self.commands,
            "description": self.description
        }
    
    @staticmethod
    def from_dict(data: dict) -> 'MacroPreset':
        """辞書から作成"""
        return MacroPreset(
            name=data.get("name", ""),
            commands=data.get("commands", []),
            description=data.get("description", "")
        )