"""Conformance test kit for connector authors."""

from usmp.testing.conformance import (
    ConformanceError,
    ConnectorConformance,
    check_capabilities,
    check_read,
    check_reader_is_read_only,
    check_rerun,
    check_round_trip,
)

__all__ = [
    "ConformanceError",
    "ConnectorConformance",
    "check_capabilities",
    "check_read",
    "check_reader_is_read_only",
    "check_rerun",
    "check_round_trip",
]
