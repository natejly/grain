from __future__ import annotations

from cryptography.fernet import Fernet

from ..config import Settings, get_settings


class EncryptionNotConfiguredError(RuntimeError):
    pass


def _fernet(settings: Settings) -> Fernet:
    if not settings.integrations_ready or settings.integrations_encryption_key is None:
        raise EncryptionNotConfiguredError(
            "INTEGRATIONS_ENCRYPTION_KEY is not configured"
        )
    return Fernet(settings.integrations_encryption_key.get_secret_value().encode())


def encrypt_secret(value: str, settings: Settings | None = None) -> str:
    return _fernet(settings or get_settings()).encrypt(value.encode()).decode()


def decrypt_secret(value: str, settings: Settings | None = None) -> str:
    return _fernet(settings or get_settings()).decrypt(value.encode()).decode()
