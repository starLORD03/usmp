"""``usmp connectors``: see which providers are installed."""

import typer
from rich.console import Console
from rich.table import Table

from usmp.sdk import registry
from usmp.sdk.connector import Connector

app = typer.Typer(help="List installed connectors.", no_args_is_help=True)
console = Console()


def _roles(cls: type[Connector]) -> str:
    roles = []
    if cls.open_reader is not Connector.open_reader:
        roles.append("source")
    if cls.open_writer is not Connector.open_writer:
        roles.append("target")
    return ", ".join(roles) or "-"


@app.command("list")
def list_() -> None:
    """Show installed connectors and what they can be used for."""
    connectors, broken = registry.discover()
    table = Table("Name", "Provider", "Use as", "Options", box=None, padding=(0, 2))
    for name, cls in connectors.items():
        options = ", ".join(sorted({*cls.config_keys, "label"}))
        table.add_row(name, cls.display_name, _roles(cls), options)
    console.print(table)
    for b in broken:
        console.print(f"[red]✗ {b.name}[/red] ({b.target}) failed to load: {b.error}")
    if broken:
        raise typer.Exit(1)
