from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def get_message_fernet() -> Fernet:
    settings = get_settings()
    return Fernet(settings.message_encryption_key.encode("utf-8"))


def encrypt_message(plaintext: str) -> str:
    token = get_message_fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_message(ciphertext: str) -> str:
    plaintext = get_message_fernet().decrypt(ciphertext.encode("utf-8"))
    return plaintext.decode("utf-8")
