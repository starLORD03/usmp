"""The ``usmp`` command."""

from typing import Annotated

import typer

from usmp import __version__
from usmp.cli import auth, bundle, connectors, migrate
from usmp.sdk import registry

app = typer.Typer(
    name="usmp",
    help="Move your data between services and platforms without losing it.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(auth.app, name="auth")
app.add_typer(bundle.app, name="bundle")
app.add_typer(connectors.app, name="connectors")
app.command("migrate")(migrate.command)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"usmp {__version__}")
        raise typer.Exit


@app.callback()
def root(
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the version and exit.",
            callback=_print_version,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Move your data between services and platforms without losing it."""


@app.command()
def info() -> None:
    """Show what this installation of USMP can do."""
    found, broken = registry.discover()
    typer.echo(f"usmp {__version__}")
    typer.echo(f"Connectors: {', '.join(found) or 'none'}")
    if broken:
        typer.echo(f"Broken connectors: {', '.join(b.name for b in broken)}")


def main() -> None:
    """Entry point for the ``usmp`` console script."""
    app()
