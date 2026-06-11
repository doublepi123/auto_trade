from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.config import settings

logger = logging.getLogger(__name__)


_PREFIX = "enc:"
_KEY_PATH = Path(os.environ.get("AUTO_TRADE_CREDENTIAL_KEY_PATH", "data/credential_private_key.pem"))


def is_encrypted(value: str) -> bool:
    return value.startswith(_PREFIX)


def encrypt_secret(value: str) -> str:
    if not value or is_encrypted(value):
        return value

    private_key = _load_private_key()
    public_key = private_key.public_key()
    data_key = os.urandom(32)
    nonce = os.urandom(12)
    ciphertext = AESGCM(data_key).encrypt(nonce, value.encode("utf-8"), None)
    wrapped_key = public_key.encrypt(
        data_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    payload = {
        "v": 1,
        "k": _encode(wrapped_key),
        "n": _encode(nonce),
        "c": _encode(ciphertext),
    }
    return _PREFIX + _encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    if not is_encrypted(value):
        return value

    private_key = _load_private_key()
    payload = json.loads(_decode(value[len(_PREFIX):]).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid encrypted payload: expected a JSON object")
    required_keys = {"v", "k", "n", "c"}
    missing = required_keys - set(payload.keys())
    if missing:
        raise ValueError(f"invalid encrypted payload: missing keys {sorted(missing)}")
    if payload["v"] != 1:
        raise ValueError(f"unsupported encrypted payload version: {payload['v']}")
    data_key = private_key.decrypt(
        _decode(payload["k"]),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return AESGCM(data_key).decrypt(_decode(payload["n"]), _decode(payload["c"]), None).decode("utf-8")


def _load_or_create_salt() -> bytes:
    """Load or create a random per-installation PBKDF2 salt stored alongside the PEM key."""
    salt_path = _KEY_PATH.parent / "credential_kek.salt"
    if salt_path.exists():
        return salt_path.read_bytes()
    salt_path.parent.mkdir(parents=True, exist_ok=True)
    salt = os.urandom(32)
    try:
        fd = os.open(salt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(salt)
    except FileExistsError:
        return salt_path.read_bytes()
    return salt


_LEGACY_SALT = b"auto_trade_credential_kek"


def _derive_kek(salt: bytes | None = None) -> bytes | None:
    """Derive a key-encrypting key from CREDENTIAL_MASTER_KEY env var via PBKDF2."""
    master_key = os.environ.get("CREDENTIAL_MASTER_KEY")
    if not master_key:
        return None
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt if salt is not None else _load_or_create_salt(),
        iterations=600_000,
    )
    return kdf.derive(master_key.encode("utf-8"))


def _load_private_key() -> rsa.RSAPrivateKey:
    if _KEY_PATH.exists():
        _KEY_PATH.chmod(0o600)
        key_bytes = _KEY_PATH.read_bytes()
        kek = _derive_kek()
        if kek is not None:
            try:
                return _load_rsa_private_key(key_bytes, kek)
            except (ValueError, TypeError):
                # Try legacy salt for backward compatibility with existing deployments
                legacy_kek = _derive_kek(salt=_LEGACY_SALT)
                if legacy_kek is not None and legacy_kek != kek:
                    try:
                        return _load_rsa_private_key(key_bytes, legacy_kek)
                    except (ValueError, TypeError):
                        pass
                raise ValueError(
                    "CREDENTIAL_MASTER_KEY does not match the key file encryption. "
                    "If the master key was changed, delete data/credential_private_key.pem "
                    "and restart to regenerate (existing encrypted credentials will be lost)."
                )
        return _load_rsa_private_key(key_bytes, None)

    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kek = _derive_kek()
    if kek is not None:
        encryption_algorithm = serialization.BestAvailableEncryption(kek)
    else:
        if settings.env not in ("dev", "test"):
            raise ValueError(
                "CREDENTIAL_MASTER_KEY must be set outside dev/test to encrypt the credential private key"
            )
        logger.warning(
            "CREDENTIAL_MASTER_KEY not set – storing credential private key without encryption. "
            "Set the env var to protect stored credentials from filesystem read access."
        )
        encryption_algorithm = serialization.NoEncryption()
    key_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=encryption_algorithm,
    )
    try:
        fd = os.open(_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as key_file:
            key_file.write(key_bytes)
    except FileExistsError:
        # Another process already created the key file; reload it.
        return _load_private_key()
    return private_key


def _load_rsa_private_key(key_bytes: bytes, password: bytes | None) -> rsa.RSAPrivateKey:
    private_key = serialization.load_pem_private_key(key_bytes, password=password)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError("credential private key must be an RSA private key")
    return private_key


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))
