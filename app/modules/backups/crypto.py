"""Encrypting a backup before it leaves the server: AES-256-GCM over 1 MB
chunks, so a file of any size is encrypted without holding it in memory.

Format: MAGIC, an 8-byte random nonce prefix, then for each chunk a 4-byte
big-endian length and the ciphertext. Chunk i's nonce is the prefix plus i;
its associated data says whether it's the last chunk, so a file cut short or
with chunks swapped fails to decrypt instead of restoring half a backup.

The key is BACKUP_ENCRYPTION_KEY: 32 random bytes, URL-safe base64
(`new_key`). Lose it and the off-server backups can't be read -- keep a copy
outside the server (a password manager).
"""
from __future__ import annotations

import base64
import os
import struct
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"HIRLUMO-BACKUP1\n"
CHUNK = 1024 * 1024
_LAST, _MORE = b"\x01", b"\x00"


class BackupKeyError(ValueError):
    """The key is missing, malformed, or doesn't open this file."""


def new_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def load_key(value: str | None) -> bytes:
    try:
        key = base64.urlsafe_b64decode((value or "").strip().encode())
    except (ValueError, TypeError):
        key = b""
    if len(key) != 32:
        raise BackupKeyError("BACKUP_ENCRYPTION_KEY must be 32 bytes in URL-safe base64 "
                             "(make one with: hirlumo backup-key)")
    return key


def _nonce(prefix: bytes, index: int) -> bytes:
    return prefix + struct.pack(">I", index)


def encrypt(source: BinaryIO, target: BinaryIO, key: bytes) -> None:
    aead = AESGCM(key)
    prefix = os.urandom(8)
    target.write(MAGIC + prefix)
    index = 0
    chunk = source.read(CHUNK)
    while True:
        following = source.read(CHUNK)
        last = not following
        sealed = aead.encrypt(_nonce(prefix, index), chunk, _LAST if last else _MORE)
        target.write(struct.pack(">I", len(sealed)) + sealed)
        if last:
            return
        chunk, index = following, index + 1


def decrypt(source: BinaryIO, target: BinaryIO, key: bytes) -> None:
    from cryptography.exceptions import InvalidTag

    if source.read(len(MAGIC)) != MAGIC:
        raise BackupKeyError("That isn't an encrypted Hirlumo backup.")
    aead = AESGCM(key)
    prefix = source.read(8)
    index = 0
    while True:
        header = source.read(4)
        if len(header) < 4:
            raise BackupKeyError("The backup is cut short (it may not have finished uploading).")
        sealed = source.read(struct.unpack(">I", header)[0])
        nonce = _nonce(prefix, index)
        try:
            target.write(aead.decrypt(nonce, sealed, _MORE))
        except InvalidTag:
            try:
                target.write(aead.decrypt(nonce, sealed, _LAST))
            except InvalidTag:
                raise BackupKeyError("Wrong BACKUP_ENCRYPTION_KEY, or the backup is damaged.") from None
            if source.read(1):
                raise BackupKeyError("The backup has data after its end -- it's damaged.")
            return
        index += 1
