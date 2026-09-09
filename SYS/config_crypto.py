"""Configuration encryption-at-rest for sensitive fields.

Uses pycryptodome (AES-GCM) with a key derived from:
1. The environment variable ``MM_CONFIG_KEY`` (preferred).
2. A machine-local keyfile at ``<repo>/.config_key`` (auto-generated on first use).

Plugins declare sensitive config keys via ``"secret": True`` in their config
schema. When a key is marked secret, its value is encrypted before being stored
in the database and decrypted on read.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from Crypto.Cipher import AES
    from Crypto.Protocol.KDF import scrypt as _scrypt
except Exception:
    AES = None  # type: ignore
    _scrypt = None  # type: ignore

logger = logging.getLogger(__name__)

_KEYFILE_NAME = ".config_key"
_KEY_LENGTH = 32
_SALT_LENGTH = 16
_NONCE_LENGTH = 12
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_ENCRYPTED_PREFIX = "$MM_ENC$"
_SECRET_SCHEMA_MARKER = "secret"

_cipher_key: Optional[bytes] = None


def _repo_root() -> Path:
    try:
        return Path(__file__).resolve().parents[1]
    except Exception:
        return Path.cwd()


def _keyfile_path() -> Path:
    return _repo_root() / _KEYFILE_NAME


def _derive_key(master_secret: bytes, salt: bytes) -> bytes:
    if _scrypt is not None:
        return _scrypt(master_secret, salt, key_len=_KEY_LENGTH, N=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    import hashlib

    return hashlib.pbkdf2_hmac("sha256", master_secret, salt, 200_000, dklen=_KEY_LENGTH)


def _load_or_create_key() -> bytes:
    env_key = str(os.environ.get("MM_CONFIG_KEY", "") or "").strip()
    if env_key:
        salt = b"medeia_macina_salt_v1"
        return _derive_key(env_key.encode("utf-8"), salt)

    keyfile = _keyfile_path()
    if keyfile.exists():
        try:
            return keyfile.read_bytes()
        except Exception as exc:
            logger.warning("Failed to read config keyfile %s: %s", keyfile, exc)

    new_key = secrets.token_bytes(_KEY_LENGTH)
    try:
        keyfile.parent.mkdir(parents=True, exist_ok=True)
        with _AtomicWrite(keyfile, new_key):
            pass
        try:
            keyfile.chmod(0o600)
        except Exception:
            pass
    except Exception as exc:
        logger.error("Failed to persist config encryption key to %s: %s", keyfile, exc)
    return new_key


class _AtomicWrite:
    def __init__(self, path: Path, data: bytes) -> None:
        self._path = path
        self._data = data

    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: Any) -> None:
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            tmp.write_bytes(self._data)
            tmp.replace(self._path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass


def _get_cipher_key() -> bytes:
    global _cipher_key
    if _cipher_key is None:
        _cipher_key = _load_or_create_key()
    return _cipher_key


def encrypt_value(plaintext: str) -> str:
    if not plaintext:
        return plaintext
    if plaintext.startswith(_ENCRYPTED_PREFIX):
        return plaintext
    key = _get_cipher_key()
    nonce = secrets.token_bytes(_NONCE_LENGTH)
    salt = secrets.token_bytes(_SALT_LENGTH)
    derived = _derive_key(key, salt)
    data = plaintext.encode("utf-8")
    if AES is not None:
        cipher = AES.new(derived, AES.MODE_GCM, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(data)
        payload = salt + nonce + tag + ciphertext
        return _ENCRYPTED_PREFIX + base64.b64encode(payload).decode("ascii")
    import hashlib
    import hmac

    stream = bytearray()
    counter = 0
    while len(stream) < len(data):
        stream.extend(hashlib.sha256(derived + nonce + counter.to_bytes(4, "big")).digest())
        counter += 1
    ciphertext = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(derived, nonce + ciphertext, hashlib.sha256).digest()[:16]
    payload = salt + nonce + tag + ciphertext
    return _ENCRYPTED_PREFIX + base64.b64encode(payload).decode("ascii")


def decrypt_value(encrypted_text: str) -> str:
    if not encrypted_text:
        return encrypted_text
    if not encrypted_text.startswith(_ENCRYPTED_PREFIX):
        return encrypted_text
    key = _get_cipher_key()
    try:
        payload = base64.b64decode(encrypted_text[len(_ENCRYPTED_PREFIX):])
    except Exception:
        logger.debug("Failed to base64-decode encrypted config value (possibly corrupt); returning raw")
        return encrypted_text
    salt = payload[:_SALT_LENGTH]
    nonce = payload[_SALT_LENGTH:_SALT_LENGTH + _NONCE_LENGTH]
    tag = payload[_SALT_LENGTH + _NONCE_LENGTH:_SALT_LENGTH + _NONCE_LENGTH + 16]
    ciphertext = payload[_SALT_LENGTH + _NONCE_LENGTH + 16:]
    try:
        derived = _derive_key(key, salt)
        if AES is not None:
            cipher = AES.new(derived, AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
        import hashlib
        import hmac

        stream = bytearray()
        counter = 0
        while len(stream) < len(ciphertext):
            stream.extend(hashlib.sha256(derived + nonce + counter.to_bytes(4, "big")).digest())
            counter += 1
        expected = hmac.new(derived, nonce + ciphertext, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(expected, tag):
            raise ValueError("mac mismatch")
        plain = bytes(a ^ b for a, b in zip(ciphertext, stream))
        return plain.decode("utf-8")
    except Exception:
        logger.debug("Failed to decrypt config value (key mismatch or corruption); returning raw")
        return encrypted_text


def is_secret_key(plugin_name: str, key: str) -> bool:
    """Return True when a plugin config field should be encrypted at rest.

    Uses schema lookup (class/module) instead of instantiating plugins via
    ``get_plugin()``, which spams "Unknown plugin" for support packages like
    ``playwright`` that live under plugins/ but are not registered Providers.
    """
    key_text = str(key or "").strip()
    if not key_text:
        return False
    key_lower = key_text.lower()
    try:
        from SYS.plugin_config import get_plugin_schema

        schema = get_plugin_schema(str(plugin_name or "").strip()) or []
        for entry in schema:
            if not isinstance(entry, dict):
                continue
            entry_key = str(entry.get("key") or "").strip()
            if not entry_key:
                continue
            if entry_key == key_text or entry_key.lower() == key_lower:
                if entry.get(_SECRET_SCHEMA_MARKER):
                    return True
                break
    except Exception:
        logger.debug("Failed to check secret status for %s/%s", plugin_name, key, exc_info=True)
    sensitive_keys = frozenset(
        {
            "access_key",
            "access_token",
            "api",
            "api_hash",
            "api_key",
            "apikey",
            "authorization",
            "bearer_token",
            "bot_token",
            "client_secret",
            "cookie",
            "cookies",
            "password",
            "passphrase",
            "private_key",
            "secret",
            "secret_key",
            "session_key",
            "token",
        }
    )
    if key_lower in sensitive_keys:
        return True
    return any(part in key_lower for part in ("password", "secret", "token", "apikey", "api_key"))


def encrypt_config_value(config: Dict[str, Any], category: str, subtype: str, key: str, value: Any) -> str:
    val_str = __import__("json").dumps(value) if not isinstance(value, str) else value
    if category == "plugin" and is_secret_key(subtype, key):
        return encrypt_value(val_str)
    return val_str


def decrypt_config_value(val_str: Any, category: str, subtype: str, key: str) -> Any:
    text = val_str if isinstance(val_str, str) else str(val_str or "")
    if category == "plugin" and is_secret_key(subtype, key):
        return decrypt_value(text)
    return val_str
