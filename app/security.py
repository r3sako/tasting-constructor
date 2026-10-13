"""Пароли, токены и хеши для защиты от повторных голосов."""

import hashlib
import hmac
import os
import secrets

from .db import DATA

# без похожих символов (0/O, 1/I/l), чтобы код можно было продиктовать
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def secret_key() -> str:
    """Ключ подписи сессий и выгрузок: из окружения либо из файла рядом с базой."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = DATA / ".secret_key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32), encoding="utf-8")
        path.chmod(0o600)
    return path.read_text(encoding="utf-8").strip()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        check = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    except ValueError:
        return False
    return hmac.compare_digest(check.hex(), digest)


def new_code(length: int = 8) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sign(body: bytes) -> str:
    return hmac.new(secret_key().encode(), body, hashlib.sha256).hexdigest()
