"""USMP: Universal Service Migration Protocol reference implementation."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("usmp-protocol")
except PackageNotFoundError:  # pragma: no cover - only when running from a raw source tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
