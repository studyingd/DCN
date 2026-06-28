import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import CREDENTIAL_SECRET_KEY

logger = logging.getLogger(__name__)

# Derive the Fernet key once at module load instead of on every encrypt/decrypt call.
_raw = CREDENTIAL_SECRET_KEY.encode("utf-8")
_FERNET_KEY = base64.urlsafe_b64encode(hashlib.sha256(_raw).digest())
_FERNET = Fernet(_FERNET_KEY)


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _FERNET.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet token.

    Returns "" on failure (corrupt ciphertext, wrong key, tampering) instead of
    raising, so callers (terminal bridge / inspection / power) degrade gracefully
    rather than crashing the request / WebSocket with HTTP 500.
    """
    if not ciphertext:
        return ""
    try:
        return _FERNET.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        logger.error("Failed to decrypt credential (key mismatch or corrupt data): %s", exc)
        return ""
