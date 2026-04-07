from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings

logger = logging.getLogger(__name__)

_PREFIX = "enc::"


def _fernet(settings: Settings) -> Fernet | None:
    key = settings.booking_data_encryption_key_value()
    if not key:
        return None
    try:
        return Fernet(key.encode("utf-8"))
    except Exception:
        logger.exception("Invalid booking data encryption key")
        return None


def encrypt_secret(value: str | None, settings: Settings) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.startswith(_PREFIX):
        return text
    f = _fernet(settings)
    if f is None:
        return text
    token = f.encrypt(text.encode("utf-8")).decode("utf-8")
    return _PREFIX + token


def decrypt_secret(value: str | None, settings: Settings) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if not text.startswith(_PREFIX):
        return text
    f = _fernet(settings)
    if f is None:
        logger.warning("Encrypted value exists but booking data encryption is unavailable")
        return None
    try:
        return f.decrypt(text[len(_PREFIX) :].encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.warning("Encrypted value could not be decrypted with current key")
        return None

