"""``usmp auth``: store and remove connector passwords in the OS keychain."""

from typing import Annotated

import typer
from keyring.errors import KeyringError
from rich.console import Console

from usmp.sdk import credentials

app = typer.Typer(help="Store passwords for connectors in your OS keychain.", no_args_is_help=True)
console = Console()

Connector = Annotated[str, typer.Argument(help="Connector name, e.g. caldav.")]
Account = Annotated[
    str,
    typer.Argument(
        help="Account, as the connector names it, e.g. me@icloud.com@https://caldav.icloud.com"
    ),
]


@app.command("set")
def set_(connector: Connector, account: Account) -> None:
    """Prompt for a password (hidden) and store it in the keychain."""
    secret = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    try:
        credentials.set_secret(connector, account, secret)
    except KeyringError as exc:
        console.print(f"[red]✗ Could not store the password: {exc}[/red]")
        raise typer.Exit(2) from exc
    console.print(f"[green]✓ Stored the password for {connector} {account}.[/green]")


@app.command()
def forget(connector: Connector, account: Account) -> None:
    """Remove a stored password."""
    if credentials.delete_secret(connector, account):
        console.print(f"[green]✓ Removed the password for {connector} {account}.[/green]")
    else:
        console.print(f"No password was stored for {connector} {account}.")
