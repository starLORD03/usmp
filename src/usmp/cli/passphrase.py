"""Asking for the passphrase of an encrypted backup (spec §7.1).

The passphrase comes from a hidden prompt or from an environment variable named on the command
line (for scripts). It is never echoed, logged, saved, or put in a printed command.
"""

import os
import sys
from pathlib import Path

import typer
from rich.console import Console

from usmp.core.encryption import MIN_PASSPHRASE, check_passphrase

console = Console()
EXIT_ERROR = 2


def _interactive() -> bool:
    return sys.stdin.isatty()


def _fail(message: str) -> typer.Exit:
    console.print(f"[red]✗ {message}[/red]")
    return typer.Exit(EXIT_ERROR)


def _from_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise _fail(f"the environment variable {name} (from --passphrase-env) is not set")
    return value


def new_passphrase(env: str | None) -> str:
    """A passphrase to encrypt a new backup with: from ``env``, or asked twice."""
    if env is not None:
        value = _from_env(env)
    elif not _interactive():
        raise _fail("to encrypt without a terminal, give the passphrase with --passphrase-env VAR")
    else:
        console.print(
            "  [dim]Choose a passphrase for the backup. Keep it safe: without it the backup "
            f"cannot be opened. At least {MIN_PASSPHRASE} characters.[/dim]"
        )
        while True:
            value = str(
                typer.prompt("  Backup passphrase", hide_input=True, confirmation_prompt=True)
            )
            try:
                check_passphrase(value)
            except ValueError as exc:
                console.print(f"  [red]{exc}[/red]")
                continue
            break
    try:
        check_passphrase(value)
    except ValueError as exc:
        raise _fail(str(exc)) from None
    return value


def existing_passphrase(bundle: Path, env: str | None) -> str:
    """The passphrase of the encrypted backup ``bundle``: from ``env``, or asked once."""
    if env is not None:
        return _from_env(env)
    if not _interactive():
        raise _fail(
            f"{bundle} is encrypted: give its passphrase with --passphrase-env VAR, or run this "
            "in a terminal"
        )
    return str(typer.prompt(f"  Passphrase for {bundle.name}", hide_input=True))


__all__ = ["existing_passphrase", "new_passphrase"]
