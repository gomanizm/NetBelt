"""パスワード暗号化/復号化モジュール

Windows の DPAPI (CryptProtectData) を用い、暗号鍵を OS・ユーザーアカウントに紐付ける。
本アプリは Windows 専用のため、DPAPI 以外の暗号化方式は持たない。
DPAPI が利用できない環境では暗号化を行わず、明示的に RuntimeError を送出する
（パスワードが平文で設定ファイルに書き出される経路を作らないため）。
"""
import base64
import sys

# DPAPI形式であることを示す接頭辞（base64本体の前に付与）
_DPAPI_PREFIX = "DPAPI:"

# --- DPAPI (Windows) ---
_DPAPI_AVAILABLE = False
if sys.platform == "win32":
    try:
        import ctypes
        from ctypes import wintypes

        class _DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        def _to_blob(data):
            buf = ctypes.create_string_buffer(data, len(data))
            return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

        def _dpapi_protect(data):
            blob_in = _to_blob(data)
            blob_out = _DATA_BLOB()
            ok = ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
            )
            if not ok:
                raise OSError("CryptProtectData failed")
            try:
                return ctypes.string_at(blob_out.pbData, blob_out.cbData)
            finally:
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)

        def _dpapi_unprotect(data):
            blob_in = _to_blob(data)
            blob_out = _DATA_BLOB()
            ok = ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
            )
            if not ok:
                raise OSError("CryptUnprotectData failed")
            try:
                return ctypes.string_at(blob_out.pbData, blob_out.cbData)
            finally:
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)

        _DPAPI_AVAILABLE = True
    except Exception:
        _DPAPI_AVAILABLE = False


class PasswordCrypto:
    """パスワードの暗号化/復号化を行うクラス

    - encrypt: DPAPIで暗号化し "DPAPI:<base64>" を返す
    - decrypt: DPAPI形式・平文のいずれも受け付ける
    """

    def encrypt(self, plain_password: str) -> str:
        """パスワードを暗号化して文字列で返す

        Raises:
            RuntimeError: DPAPI が利用できない環境（Windows 以外）で呼ばれた場合
        """
        if not plain_password:
            return ""
        if not _DPAPI_AVAILABLE:
            raise RuntimeError(
                "DPAPI が利用できないためパスワードを暗号化できません。"
                "本アプリは Windows 専用です。"
            )
        token = _dpapi_protect(plain_password.encode("utf-8"))
        return _DPAPI_PREFIX + base64.b64encode(token).decode("ascii")

    def decrypt(self, encrypted_password: str) -> str:
        """暗号化されたパスワード(または平文)を復号して返す"""
        if not encrypted_password:
            return ""
        if encrypted_password.startswith(_DPAPI_PREFIX):
            if not _DPAPI_AVAILABLE:
                # 別環境で作られたDPAPI値は復号できない。そのまま返す(接続は失敗する想定)
                return encrypted_password
            try:
                token = base64.b64decode(encrypted_password[len(_DPAPI_PREFIX):])
                return _dpapi_unprotect(token).decode("utf-8")
            except Exception:
                return encrypted_password
        # 平文
        return encrypted_password

    def is_encrypted(self, password: str) -> bool:
        """パスワードが暗号化済みか判定"""
        if not password:
            return False
        return password.startswith(_DPAPI_PREFIX)
