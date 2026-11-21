"""The ``usmp`` command."""

from typing import Annotated

import typer

from usmp import __version__

app = typer.Typer(
    name="usmp",
    help="Move your data between services and platforms without losing it.",
    no_args_is_help=True,
    add_completion=False,
)


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
    typer.echo(f"usmp {__version__}")
    typer.echo("No connectors installed yet. See https://github.com/starLORD03/usmp")


def main() -> None:
    """Entry point for the ``usmp`` console script."""
    app()
