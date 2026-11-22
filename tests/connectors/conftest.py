"""Shared fixtures for connector tests."""

import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def radicale(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    base = tmp_path_factory.mktemp("radicale")
    store, log = base / "store", base / "radicale.log"
    port = _free_port()
    with log.open("wb") as log_file:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv
            [
                sys.executable,
                "-m",
                "radicale",
                "--storage-filesystem-folder",
                str(store),
                "--auth-type",
                "none",
                "--rights-type",
                "authenticated",
                "--server-hosts",
                f"127.0.0.1:{port}",
                "--logging-level",
                "info",
            ],
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    deadline = time.monotonic() + 60
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            if time.monotonic() > deadline or proc.poll() is not None:
                proc.kill()
                pytest.fail(f"Radicale did not start:\n{log.read_text(errors='replace')[-3000:]}")
            time.sleep(0.2)
    yield f"http://127.0.0.1:{port}/", store
    proc.terminate()
    proc.wait(timeout=10)
