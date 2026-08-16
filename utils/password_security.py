import base64
import hashlib
import hmac
import re

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from flask import current_app

PASSWORD_PATTERN = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z0-9\s]).{8,}$")
_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


def _pepper():
    value = current_app.config.get("PASSWORD_PEPPER", "")
    if not value or len(value) < 32:
        raise RuntimeError("PASSWORD_PEPPER deve possuir ao menos 32 caracteres.")
    return value.encode("utf-8")


def _peppered(password):
    digest = hmac.new(_pepper(), str(password).encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def is_strong_password(password):
    return bool(PASSWORD_PATTERN.fullmatch(str(password or "")))


def hash_password(password):
    if not is_strong_password(password):
        raise ValueError("Senha fraca.")
    return _hasher.hash(_peppered(password))


def verify_password(password, stored_hash):
    try:
        valid = _hasher.verify(str(stored_hash or ""), _peppered(password))
        return bool(valid), bool(valid and _hasher.check_needs_rehash(stored_hash))
    except (VerifyMismatchError, InvalidHashError):
        return False, False
