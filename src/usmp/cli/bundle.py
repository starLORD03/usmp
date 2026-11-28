"""``usmp bundle``: inspect and validate ``.usmp`` files."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from usmp.cli.passphrase import existing_passphrase
from usmp.core.bundle import BundleError, BundleReader, is_encrypted

app = typer.Typer(help="Inspect and validate .usmp bundles.", no_args_is_help=True)
console = Console()

BundlePath = Annotated[
    Path,
    typer.Argument(exists=True, dir_okay=False, readable=True, help="Path to a .usmp bundle."),
]
PassphraseEnv = Annotated[
    str | None,
    typer.Option(
        "--passphrase-env",
        metavar="VAR",
        help="For an encrypted bundle: the environment variable holding its passphrase. "
        "Without it you are asked.",
    ),
]


def _open(path: Path, passphrase_env: str | None) -> BundleReader:
    try:
        passphrase = existing_passphrase(path, passphrase_env) if is_encrypted(path) else None
        return BundleReader(path, passphrase=passphrase)
    except BundleError as exc:
        console.print(f"[red]✗ Cannot open bundle:[/red] {exc}")
        raise typer.Exit(2) from exc


@app.command()
def inspect(
    path: BundlePath,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the raw manifest as JSON.")
    ] = False,
    passphrase_env: PassphraseEnv = None,
) -> None:
    """Show what a bundle contains."""
    with _open(path, passphrase_env) as reader:
        m = reader.manifest
        if as_json:
            typer.echo(m.model_dump_json(indent=2))
            return
        label = f" ({m.source.label})" if m.source.label else ""
        console.print(f"[bold]{path.name}[/bold]")
        console.print(f"  Source:   {m.source.display_name}{label}")
        console.print(f"  Created:  {m.created_at:%Y-%m-%d %H:%M:%S %Z} by usmp {m.tool.version}")
        table = Table("Domain", "Type", "Items", box=None, padding=(0, 2))
        for c in m.counts:
            table.add_row(c.domain, c.type, str(c.count))
        console.print(table)
        console.print(f"  Declared losses: {m.losses}    Raw originals: {m.raw_entries}")
        console.print(f"  Encrypted: {'yes' if reader.encrypted else 'no'}")


@app.command()
def validate(path: BundlePath, passphrase_env: PassphraseEnv = None) -> None:
    """Check a bundle's integrity. Exits with status 1 if anything is wrong."""
    with _open(path, passphrase_env) as reader:
        problems = reader.validate()
    if problems:
        console.print(f"[red]✗ {path.name} has {len(problems)} problem(s):[/red]")
        for problem in problems:
            console.print(f"  • {problem}")
        raise typer.Exit(1)
    console.print(f"[green]✓ {path.name} is valid.[/green]")
