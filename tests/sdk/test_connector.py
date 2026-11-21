from collections.abc import Mapping
from typing import ClassVar

import pytest

from tests.conftest import make_capabilities, make_event
from usmp.core.models import CapabilityManifest
from usmp.sdk.connector import Connector, ConnectorError, ExportRecord


class Minimal(Connector):
    name = "minimal"
    display_name = "Minimal"
    config_keys: ClassVar[Mapping[str, str]] = {"path": "where things live"}

    def capabilities(self) -> CapabilityManifest:
        return make_capabilities("minimal")


def test_unknown_options_rejected_with_accepted_list() -> None:
    with pytest.raises(ConnectorError, match=r"unknown option\(s\) colour; accepted: label, path"):
        Minimal({"colour": "red"})


def test_require_explains_missing_option() -> None:
    with pytest.raises(
        ConnectorError, match=r"missing required option 'path' \(where things live\)"
    ):
        Minimal().require("path")


def test_require_returns_value_and_label_is_info() -> None:
    c = Minimal({"path": " data/x ", "label": "Work"})

    assert c.require("path") == "data/x"
    assert c.info().label == "Work"
    assert c.info().connector == "minimal"


def test_roles_not_implemented_by_default() -> None:
    with pytest.raises(ConnectorError, match="cannot be used as a source"):
        Minimal().open_reader()
    with pytest.raises(ConnectorError, match="cannot be used as a target"):
        Minimal().open_writer()


def test_export_record_requires_raw_and_name_together() -> None:
    with pytest.raises(ValueError, match="together"):
        ExportRecord(item=make_event("1"), raw=b"x")
