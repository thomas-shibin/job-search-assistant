"""Password hashing (Argon2id, OWASP's first choice) and the strength policy."""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2id with the library's current recommended cost
# Verified against when the account doesn't exist, so a wrong email takes as
# long as a wrong password (no account enumeration by timing).
_DUMMY_HASH = _hasher.hash("not-a-real-password-0Aa!")

MIN_LENGTH = 12
MAX_LENGTH = 128
PASSWORD_RULES = (
    f"At least {MIN_LENGTH} characters. A short phrase of several words works well. "
    "It can't contain your name or email."
)
_COMMON = {
    "password", "passw0rd", "qwerty", "letmein", "welcome", "iloveyou", "admin", "monkey", "dragon",
    "football", "baseball", "sunshine", "princess", "trustno1", "123456", "12345678", "abc123", "111111",
    "1q2w3e", "asdfgh", "zxcvbn", "changeme", "secret",
}


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def password_problems(password: str, *, email: str = "", first_name: str = "", last_name: str = "") -> list[str]:
    """Why `password` isn't strong enough (empty when it is)."""
    problems = []
    if len(password) < MIN_LENGTH:
        problems.append(f"use at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        problems.append(f"use at most {MAX_LENGTH} characters")
    if password and len(set(password.lower())) < 5:
        problems.append("use more different characters")
    lowered = password.lower()
    personal = [email.split("@")[0], first_name, last_name]
    if any(part and len(part) >= 3 and part.lower() in lowered for part in personal):
        problems.append("don't include your name or email")
    if any(word in lowered for word in _COMMON):
        problems.append("avoid common words like \"password\" or \"qwerty\"")
    return problems
