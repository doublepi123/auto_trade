from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_PREFIX = "enc:"
_KEY_PATH = Path("data/credential_private_key.pem")


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
    data_key = private_key.decrypt(
        _decode(payload["k"]),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return AESGCM(data_key).decrypt(_decode(payload["n"]), _decode(payload["c"]), None).decode("utf-8")


def _load_private_key() -> rsa.RSAPrivateKey:
    if _KEY_PATH.exists():
        _KEY_PATH.chmod(0o600)
        return serialization.load_pem_private_key(_KEY_PATH.read_bytes(), password=None)

    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    fd = os.open(_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as key_file:
        key_file.write(key_bytes)
    return private_key


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))
