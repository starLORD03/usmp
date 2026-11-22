from collections.abc import Iterator

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import KeyringError, PasswordDeleteError
from typer.testing import CliRunner

from usmp.cli.app import app
from usmp.sdk import credentials


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.store.pop((service, username), None) is None:
            raise PasswordDeleteError("missing")


class BrokenKeyring(MemoryKeyring):
    def get_password(self, service: str, username: str) -> str | None:
        raise KeyringError("no backend")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise KeyringError("locked")


@pytest.fixture
def memory_keyring() -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


def test_env_var_wins_over_keychain(
    memory_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials.set_secret("caldav", "me@x", "from-keychain")
    monkeypatch.setenv("MY_PW", "from-env")

    assert credentials.get_secret("caldav", "me@x", "MY_PW") == "from-env"
    assert credentials.get_secret("caldav", "me@x", "UNSET_VAR") == "from-keychain"
    assert credentials.get_secret("caldav", "other") is None


def test_delete(memory_keyring: MemoryKeyring) -> None:
    credentials.set_secret("caldav", "me", "pw")

    assert credentials.delete_secret("caldav", "me") is True
    assert credentials.delete_secret("caldav", "me") is False


def test_unavailable_keychain_reads_as_missing() -> None:
    previous = keyring.get_keyring()
    keyring.set_keyring(BrokenKeyring())
    try:
        assert credentials.get_secret("caldav", "me") is None
        result = CliRunner().invoke(app, ["auth", "set", "caldav", "me"], input="pw\npw\n")
        assert result.exit_code == 2
        assert "Could not store the password" in result.output
    finally:
        keyring.set_keyring(previous)


def test_cli_set_and_forget(memory_keyring: MemoryKeyring) -> None:
    runner = CliRunner()

    stored = runner.invoke(app, ["auth", "set", "caldav", "me@x"], input="s3cret\ns3cret\n")
    assert stored.exit_code == 0, stored.output
    assert "s3cret" not in stored.output
    assert memory_keyring.store[("usmp", "caldav:me@x")] == "s3cret"

    assert "Removed" in runner.invoke(app, ["auth", "forget", "caldav", "me@x"]).output
    assert (
        "No password was stored" in runner.invoke(app, ["auth", "forget", "caldav", "me@x"]).output
    )
