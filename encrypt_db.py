"""SQLite DBファイルの簡易暗号化/復号スクリプト（検証用）

使用方法:
  python encrypt_db.py encrypt   # 暗号化
  python encrypt_db.py decrypt   # 復号

注意: これはファイルレベルのXOR暗号化です。本番環境ではEBS暗号化等を使用してください。
"""
import os
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "db.sqlite3"
ENCRYPTED_PATH = Path(__file__).parent / "db.sqlite3.enc"
KEY = b"caresec-encryption-key-2026!"  # 本番時は環境変数から取得


def xor_encrypt(data: bytes, key: bytes) -> bytes:
    """XOR暗号化（簡易）"""
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def encrypt():
    if not DB_PATH.exists():
        print(f"エラー: {DB_PATH} が見つかりません")
        sys.exit(1)
    if ENCRYPTED_PATH.exists():
        print(f"警告: {ENCRYPTED_PATH} が既に存在します。上書きしますか？(y/n)")
        if input().lower() != 'y':
            sys.exit(0)

    data = DB_PATH.read_bytes()
    encrypted = xor_encrypt(data, KEY)
    ENCRYPTED_PATH.write_bytes(encrypted)
    DB_PATH.unlink()
    print(f"暗号化完了: {ENCRYPTED_PATH}")


def decrypt():
    if not ENCRYPTED_PATH.exists():
        print(f"エラー: {ENCRYPTED_PATH} が見つかりません")
        sys.exit(1)

    data = ENCRYPTED_PATH.read_bytes()
    decrypted = xor_encrypt(data, KEY)
    DB_PATH.write_bytes(decrypted)
    print(f"復号完了: {DB_PATH}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("encrypt", "decrypt"):
        print("使用方法: python encrypt_db.py [encrypt|decrypt]")
        sys.exit(1)

    if sys.argv[1] == "encrypt":
        encrypt()
    else:
        decrypt()
