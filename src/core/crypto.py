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


def _is_base64(payload: str) -> bool:
    """DPAPI 本体として妥当な base64 か（空文字は不正とする）"""
    if not payload:
        return False
    try:
        # validate=True にしないと base64 以外の文字を黙って読み飛ばし、
        # 平文でも「妥当」と答えてしまう
        base64.b64decode(payload, validate=True)
    except Exception:
        return False
    return True

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
        if self.is_encrypted(encrypted_password):
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
        """パスワードが暗号化済みか判定

        接頭辞だけでは足りない。機器へ本当に "DPAPI:" で始まる
        パスワードを設定していると、平文のまま「暗号化済み」と
        見なされ、設定ファイルへ平文で書き出される。本体が base64 と
        して妥当かまで見て、そうでなければ平文として扱う。

        制限: 本体がたまたま base64 として妥当な平文（例 "DPAPI:AAAA"）は
        今も暗号文と区別できない。復号できたかどうかで判定すると、
        別環境で作られた復号不能な暗号文を平文とみなして二重に
        暗号化し、原本を失うため、そちらは採れない。
        """
        if not password:
            return False
        if not password.startswith(_DPAPI_PREFIX):
            return False
        return _is_base64(password[len(_DPAPI_PREFIX):])
