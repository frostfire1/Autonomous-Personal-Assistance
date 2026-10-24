"""Encryption at rest for stored secrets (blueprint v2 4.5): OAuth tokens are never written in the clear.

Set SECRETS_KEY to a Fernet key. Generate one with:
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Without a key nothing secret is stored: `encrypt` refuses instead of silently writing plaintext."""

from cryptography.fernet import Fernet, InvalidToken

from apa.core.settings import get_settings


class SecretsUnavailable(Exception):
    """No (or an invalid) SECRETS_KEY: secrets cannot be stored or read."""


def _fernet() -> Fernet:
    key = get_settings().secrets_key
    if not key:
        raise SecretsUnavailable("SECRETS_KEY is not set")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise SecretsUnavailable("SECRETS_KEY is not a valid Fernet key") from e


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise SecretsUnavailable("stored secret cannot be decrypted with this key") from e
