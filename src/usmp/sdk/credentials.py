"""Secrets (passwords, tokens) for connectors.

Secrets are looked up in an environment variable the user names, then in the operating system's
keychain (macOS Keychain, Windows Credential Manager, Secret Service on Linux) through
``keyring``. They are never accepted on the command line, which would leave them in shell
history, and never written to bundles, reports or logs.
"""

import os

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "usmp"


def _key(connector: str, account: str) -> str:
    return f"{connector}:{account}"


def get_secret(connector: str, account: str, env_var: str | None = None) -> str | None:
    """The secret for ``account``, or ``None`` if none is stored."""
    if env_var:
        value = os.environ.get(env_var)
        if value:
            return value
    try:
        return keyring.get_password(SERVICE, _key(connector, account))
    except KeyringError:
        return None


def set_secret(connector: str, account: str, secret: str) -> None:
    keyring.set_password(SERVICE, _key(connector, account), secret)


def delete_secret(connector: str, account: str) -> bool:
    """Remove a stored secret; ``False`` if there was none."""
    try:
        keyring.delete_password(SERVICE, _key(connector, account))
    except PasswordDeleteError:
        return False
    return True
