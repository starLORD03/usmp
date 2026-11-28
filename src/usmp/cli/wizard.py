"""Guided ``usmp migrate``: asks for each choice in plain language instead of flags.

Every answer maps to a ``--from``/``--to`` connector spec, and the equivalent command is shown at
the end so the same migration can be repeated without questions.
"""

import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import typer
from keyring.errors import KeyringError
from rich.console import Console

from usmp.core.selection import SelectionError, make_selection
from usmp.sdk import credentials, registry
from usmp.sdk.connector import ConnectorError

console = Console()


@dataclass
class Endpoint:
    """One side of a migration, as chosen in the wizard."""

    config: dict[str, str]
    name: str
    #: Settings kept out of the printed command (a password held only for this run).
    private_keys: set[str] = field(default_factory=set)

    @property
    def spec(self) -> str:
        public = {k: v for k, v in self.config.items() if k not in self.private_keys}
        return self.name + "".join(
            f"{':' if i == 0 else ','}{k}={v}" for i, (k, v) in enumerate(public.items())
        )


@dataclass(frozen=True)
class Choice:
    title: str
    ask: Callable[[str], Endpoint]
    connector: str


def _ask(text: str, default: str | None = None) -> str:
    while True:
        value = str(typer.prompt(text, default=default or "", show_default=bool(default))).strip()
        if value:
            return value
        console.print("  [yellow]Please enter a value.[/yellow]")


def _ask_optional(text: str) -> str:
    return str(typer.prompt(f"{text} (press Enter to skip)", default="", show_default=False))


def _password_env(name: str) -> str:
    return f"USMP_WIZARD_{name.upper()}_PASSWORD"


def _caldav(url: str | None, account_hint: str, password_hint: str) -> Callable[[str], Endpoint]:
    def ask(role: str) -> Endpoint:
        server = url or _ask("CalDAV server address (https://…)")
        username = _ask(account_hint)
        config = {"url": server, "username": username}
        calendar = _ask_optional(
            "Only this calendar"
            if role == "source"
            else "Put everything into this existing calendar (default: create new calendars)"
        )
        if calendar:
            config["calendar"] = calendar
        endpoint = Endpoint(config, "caldav")
        account = f"{username}@{server}"
        if credentials.get_secret("caldav", account):
            console.print(f"  Using the password saved in your keychain for {username}.")
            return endpoint
        console.print(f"  {password_hint}")
        password = str(typer.prompt("  Password", hide_input=True))
        if typer.confirm("  Save it in your system keychain for next time?", default=True):
            try:
                credentials.set_secret("caldav", account, password)
                return endpoint
            except KeyringError as exc:
                console.print(f"  [yellow]Could not save it ({exc}); using it for this run.[/]")
        env = _password_env(role)
        os.environ[env] = password
        endpoint.config["password_env"] = env
        endpoint.private_keys.add("password_env")
        return endpoint

    return ask


def _nextcloud(role: str) -> Endpoint:
    base = _ask("Nextcloud address, e.g. https://cloud.example.com").rstrip("/")
    return _caldav(
        f"{base}/remote.php/dav",
        "Nextcloud user name",
        "Use an app password: Nextcloud → Settings → Security → Create new app password.",
    )(role)


def _google(role: str) -> Endpoint:
    from usmp.connectors import gcal

    write = role == "target"
    account = _ask("Google account email")
    have = credentials.get_secret(gcal.NAME, gcal.token_account(account, write)) or (
        not write and credentials.get_secret(gcal.NAME, gcal.token_account(account, True))
    )
    if not have:
        console.print(
            "  You need to sign in once. This uses your own Google OAuth client file "
            "(see the Google guide: https://starlord03.github.io/usmp/guides/google/)."
        )
        secrets = Path(_ask("  Path to client_secret.json")).expanduser()
        if not secrets.is_file():
            raise ConnectorError(f"no file at {secrets}; download it from Google Cloud first")
        browser = typer.confirm("  Open the sign-in page in your browser now?", default=True)
        if not browser:
            console.print("  Open the link below in any browser and approve.")
        signed_in, token = gcal.sign_in(str(secrets), write=write, open_browser=browser)
        credentials.set_secret(gcal.NAME, gcal.token_account(signed_in, write), token)
        if signed_in != account:
            console.print(f"  Signed in as {signed_in}; using that account.")
        account = signed_in
        console.print(f"  [green]✓ Signed in to Google as {account}.[/green]")
    config = {"account": account}
    calendar = _ask_optional(
        "Only this calendar"
        if role == "source"
        else "Put everything into this existing calendar (default: create new calendars)"
    )
    if calendar:
        config["calendar"] = calendar
    return Endpoint(config, "google")


def _ics(role: str) -> Endpoint:
    if role == "source":
        while True:
            path = Path(_ask("Path to the .ics file, or a folder of .ics files")).expanduser()
            if path.exists():
                break
            console.print(f"  [yellow]Nothing found at {path}. Try again.[/yellow]")
    else:
        path = Path(
            _ask("Folder to create the .ics files in (one per calendar)", "usmp-calendars")
        ).expanduser()
    return Endpoint({"path": str(path)}, "ics")


def _other(role: str) -> Endpoint:
    found, _ = registry.discover()
    console.print("  Installed connectors: " + ", ".join(sorted(found)))
    while True:
        spec = _ask("Connector, e.g. jsondir:path=./backup")
        try:
            name, config = registry.parse_spec(spec)
            registry.get(name)(config)
        except ConnectorError as exc:
            console.print(f"  [yellow]{exc}[/yellow]")
            continue
        return Endpoint(config, name)


CHOICES = [
    Choice("Google Calendar", _google, "google"),
    Choice(
        "iCloud (Apple Calendar)",
        _caldav(
            "https://caldav.icloud.com",
            "Apple ID email",
            "Use an app-specific password from https://account.apple.com → Sign-In and Security.",
        ),
        "caldav",
    ),
    Choice("Nextcloud", _nextcloud, "caldav"),
    Choice(
        "Fastmail",
        _caldav(
            "https://caldav.fastmail.com/dav/",
            "Fastmail email",
            "Use an app password: Fastmail → Settings → Privacy & Security → App passwords.",
        ),
        "caldav",
    ),
    Choice(
        "Other CalDAV server",
        _caldav(None, "User name", "Enter the password for this account."),
        "caldav",
    ),
    Choice("Calendar file (.ics)", _ics, "ics"),
    Choice("Another installed connector", _other, ""),
]


def _choose(role: str) -> Endpoint:
    found, _ = registry.discover()
    available = [c for c in CHOICES if not c.connector or c.connector in found]
    question = "Where is your calendar now?" if role == "source" else "Where should it go?"
    console.print(f"\n[bold]{question}[/bold]")
    for n, choice in enumerate(available, 1):
        console.print(f"  {n}. {choice.title}")
    while True:
        picked = str(typer.prompt("Choose a number")).strip()
        if picked.isdigit() and 1 <= int(picked) <= len(available):
            return available[int(picked) - 1].ask(role)
        console.print(f"  [yellow]Enter a number from 1 to {len(available)}.[/yellow]")


def run() -> tuple[Endpoint, Endpoint]:
    """Ask for the source and the target. Raises ``typer.Abort`` if the user stops."""
    console.print(
        "[bold]USMP migration wizard[/bold]\n"
        "Copies your calendars to another service, then checks that everything arrived.\n"
        "Your current calendar is only read, never changed. Press Ctrl+C at any time to stop."
    )
    source = _choose("source")
    target = _choose("target")
    return source, target


def date_range() -> tuple[str | None, str | None]:
    """Ask for an optional date range: ``(since, until)``, each ``None`` when left open."""
    console.print(
        "\n[bold]Which events?[/bold] Recurring events are always moved whole. Examples: "
        "2025-01-01..2025-12-31, 2025-01-01.. (from then on), ..2024-12-31 (up to then)."
    )
    while True:
        answer = _ask_optional("Only events in this date range").strip()
        if not answer:
            return None, None
        first, sep, second = answer.partition("..")
        since, until = (first.strip() or None, second.strip() or None) if sep else (answer, None)
        try:
            make_selection(since, until)
        except SelectionError as exc:
            console.print(f"  [yellow]{exc}. Try again.[/yellow]")
            continue
        return since, until


def command_line(
    source: Endpoint,
    target: Endpoint,
    *,
    since: str | None = None,
    until: str | None = None,
    calendars: list[str] | None = None,
) -> str:
    """The non-interactive command for the same migration."""
    extra = "".join(
        f" {flag} {shlex.quote(value)}"
        for flag, value in [
            ("--since", since),
            ("--until", until),
            *(("--calendar", name) for name in calendars or []),
        ]
        if value
    )
    return f"usmp migrate --from '{source.spec}' --to '{target.spec}' --domain calendar{extra}"
