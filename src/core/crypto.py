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

# CryptProtectData が返す blob の先頭 20 バイト（version 1 + プロバイダ GUID）。
# 実測: encrypt() の出力を base64 復号すると、長さによらず常にこれで始まる。
_DPAPI_BLOB_HEADER = bytes.fromhex("01000000d08c9ddf0115d1118c7a00c04fc297eb")


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

    def is_dpapi_ciphertext(self, password) -> bool:
        """本当に DPAPI で包まれた値か（他の PC・アカウントのものも含む）。

        is_encrypted() は「"DPAPI:" + base64 らしさ」までしか見ないので、
        "DPAPI:cisco123" のような平文の合言葉も True になる。復号できな
        かった機器を覚えて接続を断る用途では、それを暗号文と取り違えると
        本当のパスワードを持つ機器へ繋げなくなる。base64 の中身が DPAPI の
        blob ヘッダで始まるかまで見て区別する。

        ヘッダが変わる環境があっても、ここが False に倒れるだけで、
        暗号文をそのまま送る従来どおりの動きに戻るだけにとどまる。
        """
        if not isinstance(password, str) or not self.is_encrypted(password):
            return False
        try:
            raw = base64.b64decode(password[len(_DPAPI_PREFIX):])
        except Exception:
            return False
        return raw.startswith(_DPAPI_BLOB_HEADER)

    def is_encrypted(self, password: str) -> bool:
        """パスワードが暗号化済みか判定

        接頭辞だけでは足りない。機器へ本当に "DPAPI:" で始まる
        パスワードを設定していると、平文のまま「暗号化済み」と
        見なされ、設定ファイルへ平文で書き出される。本体が base64 と
        して妥当かまで見て、そうでなければ平文として扱う。

        制限: 本体が base64 として妥当な平文は今も暗号文と区別できない。
        これは珍しい形ではなく、実測で "DPAPI:pass" / "DPAPI:Secret12" /
        "DPAPI:cisco123" / "DPAPI:AAAA" はいずれもここで True になり、
        config.json へ平文のまま書き出される。

        採らなかった案:
        ・復号できたかどうかで判定する: 別環境で作られた復号不能な暗号文を
          平文とみなして二重に暗号化し、原本を失う。
        ・復号不能な値を別キーへ退避し、再暗号化の対象から外す: 判定を
          推測から状態へ置き換えられるが、読み込みを経ずに config を組み立てて
          保存する経路には退避情報が無く、そこで暗号文を二重に包んでしまう。
          tests/test_password_reencrypt.py が固定している契約を壊すので、
          ここだけを直す変更では採らない。
        ・入力側で弾く: ui/dialogs/device_dialog.py で、変更されたパスワードが
          この判定に当たるときは確認を出すようにした。新しく入力される平文は
          これで黙って平文保存されることはない。ただし既に config.json に
          ある値には効かない。FTP/SFTP サーバーの欄は、この判定に当たると
          起動を断り理由を出すので、黙って壊れる経路にはなっていない。
        """
        if not password:
            return False
        if not password.startswith(_DPAPI_PREFIX):
            return False
        return _is_base64(password[len(_DPAPI_PREFIX):])
