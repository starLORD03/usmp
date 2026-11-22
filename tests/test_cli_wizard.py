import os
from pathlib import Path
from typing import Any

import pytest
from keyring.errors import KeyringError
from typer.testing import CliRunner

from usmp.cli import migrate as migrate_cli
from usmp.cli import wizard
from usmp.cli.app import app
from usmp.connectors import gcal
from usmp.core.engine import MigrationError
from usmp.sdk import credentials
from usmp.sdk.connector import Connector

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ics"
# Menu numbers, in the order the wizard lists them.
GOOGLE, ICLOUD, NEXTCLOUD, FASTMAIL, CALDAV, ICS, OTHER = range(1, 8)
ENV = "USMP_WIZARD_TARGET_PASSWORD"


@pytest.fixture(autouse=True)
def interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrate_cli, "_interactive", lambda: True)


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(credentials, "get_secret", lambda c, a, env=None: store.get((c, a)))
    monkeypatch.setattr(credentials, "set_secret", lambda c, a, s: store.__setitem__((c, a), s))
    return store


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[Connector]:
    """Stop at the engine and keep the connectors the wizard built."""
    found: list[Connector] = []

    def fake_migrate(source: Connector, target: Connector, **kwargs: Any) -> None:
        found.extend([source, target])
        raise MigrationError("stopped by the test")

    monkeypatch.setattr(migrate_cli, "migrate", fake_migrate)
    return found


def answers(*lines: object) -> str:
    return "".join(f"{line}\n" for line in lines)


def run(tmp_path: Path, text: str, *extra: str) -> Any:
    return runner.invoke(
        app,
        [
            "migrate",
            "--bundle",
            str(tmp_path / "b.usmp"),
            "--report-dir",
            str(tmp_path / "reports"),
            *extra,
        ],
        input=text,
    )


def test_ics_to_ics_end_to_end(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = run(
        tmp_path,
        answers(
            9,  # not on the menu
            ICS,
            tmp_path / "missing.ics",  # does not exist: asked again
            FIXTURES / "google.ics",
            ICS,
            out,
            "y",  # write to the target
        ),
    )

    assert result.exit_code == 0, result.output
    assert "Enter a number from 1 to 7" in result.output
    assert "Nothing found at" in result.output
    assert "Migration verified" in result.output
    assert f"usmp migrate --from 'ics:path={FIXTURES / 'google.ics'}'" in result.output
    assert (out / "Personal.ics").exists()


def test_declining_at_the_preview_writes_nothing(tmp_path: Path) -> None:
    result = run(tmp_path, answers(ICS, FIXTURES / "google.ics", ICS, tmp_path / "out", "n"))

    assert result.exit_code == 3, result.output
    assert not (tmp_path / "out").exists()


def test_stopping_mid_way_is_a_cancel(tmp_path: Path) -> None:
    result = run(tmp_path, answers(ICS))  # input ends, as with Ctrl+D / Ctrl+C

    assert result.exit_code == 3, result.output
    assert "Cancelled" in result.output


def test_icloud_password_is_saved_in_the_keychain(
    tmp_path: Path, keychain: dict[tuple[str, str], str], captured: list[Connector]
) -> None:
    result = run(tmp_path, answers(ICLOUD, "me@icloud.com", "", "app-pw", "y", ICS, tmp_path))

    assert result.exit_code == 2, result.output
    assert "Nothing was written to the target." in result.output
    source = captured[0]
    assert source.config == {"url": "https://caldav.icloud.com", "username": "me@icloud.com"}
    assert keychain[("caldav", "me@icloud.com@https://caldav.icloud.com")] == "app-pw"


def test_saved_password_is_reused_and_nextcloud_url_is_built(
    tmp_path: Path, keychain: dict[tuple[str, str], str], captured: list[Connector]
) -> None:
    keychain[("caldav", "me@https://cloud.example.com/remote.php/dav")] = "pw"
    result = run(
        tmp_path,
        answers(NEXTCLOUD, "https://cloud.example.com/", "me", "Work", ICS, tmp_path),
    )

    assert "Using the password saved" in result.output, result.output
    assert captured[0].config == {
        "url": "https://cloud.example.com/remote.php/dav",
        "username": "me",
        "calendar": "Work",
    }


@pytest.mark.parametrize("keychain_works", [False, True])
def test_unsaved_password_is_used_for_this_run_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keychain: dict[tuple[str, str], str],
    captured: list[Connector],
    keychain_works: bool,
) -> None:
    def broken(*args: object) -> None:
        raise KeyringError("locked")

    if not keychain_works:
        monkeypatch.setattr(credentials, "set_secret", broken)
    save = "y" if not keychain_works else "n"
    monkeypatch.delenv(ENV, raising=False)
    result = run(
        tmp_path,
        answers(
            ICS, FIXTURES / "google.ics", CALDAV, "https://dav.example", "u", "Cal", "s3", save
        ),
    )

    target = captured[1]
    env = target.config["password_env"]
    assert env == ENV, result.output
    assert target.config["calendar"] == "Cal"
    assert os.environ[env] == "s3"
    assert not keychain
    monkeypatch.delenv(ENV)


def test_printed_command_never_holds_the_password() -> None:
    endpoint = wizard.Endpoint(
        {"url": "https://dav.example", "username": "u", "password_env": "X"},
        "caldav",
        {"password_env"},
    )
    assert endpoint.spec == "caldav:url=https://dav.example,username=u"
    assert wizard.command_line(endpoint, wizard.Endpoint({"path": "p"}, "ics")) == (
        "usmp migrate --from 'caldav:url=https://dav.example,username=u' "
        "--to 'ics:path=p' --domain calendar"
    )


def test_google_target_signs_in_with_write_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keychain: dict[tuple[str, str], str],
    captured: list[Connector],
) -> None:
    secrets = tmp_path / "client_secret.json"
    secrets.write_text("{}")
    calls: list[tuple[str, bool, bool]] = []

    def fake_sign_in(path: str, *, write: bool, open_browser: bool) -> tuple[str, str]:
        calls.append((path, write, open_browser))
        return "real@gmail.com", "token"

    monkeypatch.setattr(gcal, "sign_in", fake_sign_in)
    result = run(
        tmp_path,
        answers(ICS, FIXTURES / "google.ics", GOOGLE, "typo@gmail.com", secrets, "n", ""),
    )

    assert calls == [(str(secrets), True, False)], result.output
    assert "Signed in as real@gmail.com; using that account." in result.output
    assert keychain[("google", "real@gmail.com:write")] == "token"
    assert captured[1].config == {"account": "real@gmail.com"}


def test_google_source_reuses_a_write_token(
    tmp_path: Path, keychain: dict[tuple[str, str], str], captured: list[Connector]
) -> None:
    keychain[("google", "me@gmail.com:write")] = "token"
    result = run(tmp_path, answers(GOOGLE, "me@gmail.com", "Work", ICS, tmp_path))

    assert captured[0].config == {"account": "me@gmail.com", "calendar": "Work"}, result.output


def test_google_without_a_client_file_explains(
    tmp_path: Path, keychain: dict[tuple[str, str], str]
) -> None:
    result = run(tmp_path, answers(GOOGLE, "me@gmail.com", tmp_path / "nope.json"))

    assert result.exit_code == 2
    assert "download it from Google Cloud" in result.output


def test_other_connector_is_checked(tmp_path: Path, captured: list[Connector]) -> None:
    result = run(
        tmp_path,
        answers(OTHER, "jsondir:wrong=1", f"jsondir:path={tmp_path},domain=calendar", ICS, "out"),
    )

    assert "unknown option" in result.output
    assert captured[0].config == {"path": str(tmp_path), "domain": "calendar"}


def test_not_a_terminal_needs_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrate_cli, "_interactive", lambda: False)
    result = run(tmp_path, "")

    assert result.exit_code == 2
    assert "run `usmp migrate` in a terminal to be guided" in " ".join(result.output.split())


def test_one_side_only_is_an_error(tmp_path: Path) -> None:
    result = run(tmp_path, "", "--from", "ics:path=x.ics")

    assert result.exit_code == 2
    assert "Missing --to" in result.output
