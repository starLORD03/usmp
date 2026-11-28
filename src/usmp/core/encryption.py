"""Passphrase encryption of backup bundles (spec §7.1, ADR 0007).

An encrypted bundle is an ordinary bundle wrapped in the `age <https://age-encryption.org/v1>`_
format with a passphrase (scrypt) recipient. Anyone with the passphrase can also open it with the
standard ``age`` or ``rage`` tools: ``age -d backup.usmp > plain.usmp``. Nothing here implements
cryptography itself; it calls pyrage, the Python binding of the Rust ``age`` implementation.

The passphrase is never stored, logged or put in an error message.
"""

from pathlib import Path

import pyrage  # type: ignore[import-untyped]

AGE_MAGIC = b"age-encryption.org/v1\n"
"""The first bytes of every age file. Plain bundles are ZIP files and start with ``PK``."""

MIN_PASSPHRASE = 8


class DecryptionError(Exception):
    """The passphrase is wrong, or the encrypted file is damaged."""


def is_encrypted_bytes(head: bytes) -> bool:
    return head.startswith(AGE_MAGIC)


def is_encrypted(path: Path) -> bool:
    """True if the file at ``path`` is age-encrypted. Raises ``OSError`` if it cannot be read."""
    with Path(path).open("rb") as fh:
        return is_encrypted_bytes(fh.read(len(AGE_MAGIC)))


def check_passphrase(passphrase: str) -> None:
    """Refuse passphrases too short to protect a whole calendar."""
    if len(passphrase) < MIN_PASSPHRASE:
        raise ValueError(f"use a passphrase of at least {MIN_PASSPHRASE} characters")


def encrypt(data: bytes, passphrase: str) -> bytes:
    check_passphrase(passphrase)
    result: bytes = pyrage.passphrase.encrypt(data, passphrase)
    return result


def decrypt(data: bytes, passphrase: str) -> bytes:
    try:
        result: bytes = pyrage.passphrase.decrypt(data, passphrase)
    except pyrage.DecryptError as exc:
        raise DecryptionError("wrong passphrase, or the backup is damaged") from exc
    return result


__all__ = [
    "AGE_MAGIC",
    "MIN_PASSPHRASE",
    "DecryptionError",
    "check_passphrase",
    "decrypt",
    "encrypt",
    "is_encrypted",
    "is_encrypted_bytes",
]
