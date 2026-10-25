"""Password hashing (Argon2id, OWASP's first choice) and the strength policy."""
from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2id with the library's current recommended cost
# Verified against when the account doesn't exist, so a wrong email takes as
# long as a wrong password (no account enumeration by timing).
_DUMMY_HASH = _hasher.hash("not-a-real-password-0Aa!")

# Google's bar, and NIST SP 800-63B's: at least 8 characters, any mix, and not
# a password everyone guesses first. No character-class rules -- they make
# passwords harder to remember, not harder to guess.
MIN_LENGTH = 8
MAX_LENGTH = 128
PASSWORD_RULES = f"At least {MIN_LENGTH} characters. Avoid common passwords like \"password123\"."
# Refused as the whole password, or as its letters with only digits/symbols
# added ("Password1!", "qwerty2024").
_COMMON = {
    "password", "passw0rd", "qwerty", "qwertyuiop", "letmein", "welcome", "iloveyou", "admin", "monkey",
    "dragon", "football", "baseball", "sunshine", "princess", "trustno", "abc", "asdfgh", "asdfghjkl",
    "zxcvbn", "zxcvbnm", "changeme", "secret", "qazwsx", "login", "master", "hello", "freedom", "whatever",
    "starwars", "superman", "batman", "computer", "internet", "shadow", "michael", "jennifer", "canada",
}
_COMMON_DIGITS = {"12345678", "123456789", "1234567890", "87654321", "11111111", "00000000", "12121212",
                  "11223344", "123123123", "01234567"}


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def password_problems(password: str) -> list[str]:
    """Why `password` can't be used (empty when it can)."""
    problems = []
    if len(password) < MIN_LENGTH:
        problems.append(f"use at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        problems.append(f"use at most {MAX_LENGTH} characters")
    lowered = password.lower()
    letters = re.sub(r"[^a-z]", "", lowered)
    if lowered in _COMMON_DIGITS or lowered in _COMMON or letters in _COMMON or len(set(lowered)) < 3:
        problems.append("this password is too common or easy to guess")
    return problems
