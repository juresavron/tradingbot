"""Password hashing for the dashboard's single, operator-provisioned owner."""

from __future__ import annotations

import hashlib
import hmac
import secrets


_PREFIX = "scrypt$32768$8$3$"


def normalize_email(value: str) -> str:
    email = value.strip().casefold()
    if not 3 <= len(email) <= 254 or email.count("@") != 1 or any(c.isspace() for c in email):
        raise ValueError("Configure a valid owner email address")
    local, domain = email.split("@")
    if not local or not domain or "." not in domain:
        raise ValueError("Configure a valid owner email address")
    return email


def _hash_parts(encoded: str) -> tuple[bytes, bytes]:
    if not isinstance(encoded, str) or not encoded.startswith(_PREFIX):
        raise ValueError("Configure a supported owner password hash")
    try:
        salt_hex, digest_hex = encoded[len(_PREFIX):].split("$")
        salt, digest = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        raise ValueError("Configure a valid owner password hash") from None
    if len(salt) != 16 or len(digest) != 32:
        raise ValueError("Configure a valid owner password hash")
    return salt, digest


def validate_password_hash(encoded: str) -> None:
    _hash_parts(encoded)


def _derive(password: str, salt: bytes) -> bytes:
    # 32 MiB memory, parallelism 3; allow room for OpenSSL overhead.
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=32768, r=8, p=3,
                          maxmem=64 * 1024 * 1024, dklen=32)


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError("Use a password containing 12-256 characters")
    salt = secrets.token_bytes(16)
    return _PREFIX + salt.hex() + "$" + _derive(password, salt).hex()


def verify_password(password: str, encoded: str) -> bool:
    salt, expected = _hash_parts(encoded)
    if not isinstance(password, str) or not 1 <= len(password) <= 256:
        return False
    return hmac.compare_digest(_derive(password, salt), expected)
