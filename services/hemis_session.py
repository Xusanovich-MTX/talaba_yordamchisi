"""Encrypt HEMIS session data before it is written to the database."""

import json

from cryptography.fernet import Fernet, InvalidToken

from config import HEMIS_SESSION_KEY


def _fernet() -> Fernet:
    if not HEMIS_SESSION_KEY:
        raise RuntimeError("HEMIS_SESSION_KEY is not configured.")
    try:
        return Fernet(HEMIS_SESSION_KEY.encode("ascii"))
    except (UnicodeEncodeError, ValueError) as error:
        raise RuntimeError("HEMIS_SESSION_KEY is invalid.") from error


def encrypt_hemis_session(session_data: dict[str, str]) -> str:
    payload = json.dumps(session_data, separators=(",", ":")).encode("utf-8")
    return _fernet().encrypt(payload).decode("ascii")


def decrypt_hemis_session(ciphertext: str) -> dict[str, str]:
    try:
        payload = _fernet().decrypt(ciphertext.encode("ascii"))
        session_data = json.loads(payload.decode("utf-8"))
    except (InvalidToken, UnicodeEncodeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Stored HEMIS session is invalid or cannot be decrypted.") from error

    if not isinstance(session_data, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in session_data.items()
    ):
        raise ValueError("Stored HEMIS session has an invalid format.")
    return session_data