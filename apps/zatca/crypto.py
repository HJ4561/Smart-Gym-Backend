"""
Secrets at rest: the device's private key and the ZATCA tokens are stored encrypted with
ZATCA_ENCRYPTION_KEY (a Fernet key in the environment). A database dump alone reveals nothing.

Generate a key once:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _fernet():
    key = getattr(settings, 'ZATCA_ENCRYPTION_KEY', '') or ''
    if not key:
        raise ImproperlyConfigured('ZATCA_ENCRYPTION_KEY is not set — add it to the environment before using ZATCA.')
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured('ZATCA_ENCRYPTION_KEY is not a valid Fernet key.') from exc


def key_configured():
    try:
        _fernet()
        return True
    except ImproperlyConfigured:
        return False


def encrypt(text):
    if not text:
        return ''
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token):
    if not token:
        return ''
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ImproperlyConfigured('A ZATCA secret could not be decrypted — was ZATCA_ENCRYPTION_KEY changed?') from exc
