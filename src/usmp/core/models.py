"""Core protocol models.

These models are the source of truth for the JSON Schemas published in ``spec/schemas/core/``.
Changing a field here changes the protocol: regenerate the schemas (``python -m usmp.core.schemas``)
and update ``spec/usmp-spec.md`` in the same pull request.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PositiveInt,
    computed_field,
    model_validator,
)

from usmp.core.hashing import content_hash, project, usmp_id_for

# --- Shared field types ------------------------------------------------------------------------

Name = Annotated[
    str,
    Field(
        pattern=r"^[a-z][a-z0-9_]*$",
        max_length=64,
        description="Lower-case identifier: letters, digits and underscores; starts with a letter.",
    ),
]
UsmpId = Annotated[
    str,
    Field(
        pattern=r"^usmp:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        description="Stable item identifier, derived from the connector and native id it was "
        "first read from.",
    ),
]
Sha256 = Annotated[
    str,
    Field(pattern=r"^sha256:[0-9a-f]{64}$", description="SHA-256 digest, prefixed `sha256:`."),
]
FieldName = Annotated[str, Field(min_length=1, max_length=128)]


def _sorted_unique(values: list[str]) -> list[str]:
    return sorted(set(values))


FieldList = Annotated[list[FieldName], AfterValidator(_sorted_unique)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- Items -------------------------------------------------------------------------------------


class SourceRef(_Model):
    """Where an item came from."""

    connector: Name
    native_id: str = Field(min_length=1, description="The provider's own identifier.")


class Item(_Model):
    """One record in a domain's canonical model, wrapped in the USMP envelope."""

    usmp_id: UsmpId
    domain: Name = Field(description="Domain, e.g. `calendar`.")
    type: Name = Field(description="Item type within the domain, e.g. `event`.")
    schema_version: PositiveInt = Field(description="Major version of the domain schema.")
    source: SourceRef
    parent: UsmpId | None = Field(
        default=None, description="`usmp_id` of the containing item, e.g. an event's calendar."
    )
    payload: dict[str, JsonValue] = Field(
        description="The item in the domain's canonical schema. Top-level keys are the "
        "*fields* used by capability manifests and verification."
    )
    extensions: dict[Name, dict[str, JsonValue]] = Field(
        default_factory=dict,
        description="Provider-specific data keyed by connector name. Not hashed or verified.",
    )
    content_hash: Sha256 = Field(
        description="Hash of `domain`, `type`, `schema_version` and `payload`."
    )

    @classmethod
    def create(
        cls,
        *,
        connector: str,
        native_id: str,
        domain: str,
        type: str,
        schema_version: int,
        payload: Mapping[str, Any],
        parent: str | None = None,
        extensions: Mapping[str, Mapping[str, Any]] | None = None,
        usmp_id: str | None = None,
    ) -> Self:
        """Build an item, deriving ``usmp_id`` (unless given) and ``content_hash``."""
        return cls(
            usmp_id=usmp_id or usmp_id_for(connector, native_id),
            domain=domain,
            type=type,
            schema_version=schema_version,
            source=SourceRef(connector=connector, native_id=native_id),
            parent=parent,
            payload=dict(payload),
            extensions={k: dict(v) for k, v in (extensions or {}).items()},
            content_hash=content_hash(
                domain=domain, type=type, schema_version=schema_version, payload=payload
            ),
        )

    @model_validator(mode="after")
    def _check_content_hash(self) -> Self:
        expected = content_hash(
            domain=self.domain,
            type=self.type,
            schema_version=self.schema_version,
            payload=self.payload,
        )
        if self.content_hash != expected:
            raise ValueError(
                f"content_hash does not match payload (expected {expected}); "
                "the item was modified or corrupted"
            )
        return self

    def projected_hash(self, fields: list[str] | set[str] | frozenset[str]) -> str:
        """Hash of this item restricted to ``fields``: used to compare comparable fields only."""
        return content_hash(
            domain=self.domain,
            type=self.type,
            schema_version=self.schema_version,
            payload=project(self.payload, fields),
        )


# --- Losses ------------------------------------------------------------------------------------


class LossReason(StrEnum):
    UNSUPPORTED_BY_CANONICAL = "unsupported_by_canonical"
    """The source has data the canonical model cannot represent."""
    UNSUPPORTED_BY_TARGET = "unsupported_by_target"
    """The target connector cannot write this field."""
    NOT_READABLE_BY_TARGET = "not_readable_by_target"
    """The target can write the field but not read it back, so it cannot be verified."""
    VALUE_CONVERTED = "value_converted"
    """The value was kept but changed form, e.g. rounded or reformatted."""


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class LossRecord(_Model):
    """A field of an item that will not arrive (or not arrive unchanged) at the target."""

    usmp_id: UsmpId
    field: str = Field(min_length=1, description="Top-level payload field, or a provider field.")
    reason: LossReason
    severity: Severity = Severity.WARNING
    stage: Literal["export", "plan", "import"]
    detail: str = ""


# --- Capabilities ------------------------------------------------------------------------------


class TypeCapability(_Model):
    type: Name
    read_fields: FieldList = Field(
        default_factory=list, description="Payload fields this connector can read."
    )
    write_fields: FieldList = Field(
        default_factory=list, description="Payload fields this connector can write."
    )


class DomainCapability(_Model):
    domain: Name
    schema_version: PositiveInt
    types: list[TypeCapability]

    @model_validator(mode="after")
    def _unique_types(self) -> Self:
        names = [t.type for t in self.types]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate types in domain {self.domain!r}: {names}")
        return self


class CapabilityManifest(_Model):
    """What a connector can read and write."""

    connector: Name
    display_name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    domains: list[DomainCapability]

    @model_validator(mode="after")
    def _unique_domains(self) -> Self:
        names = [d.domain for d in self.domains]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate domains: {names}")
        return self

    def domain(self, domain: str) -> DomainCapability | None:
        return next((d for d in self.domains if d.domain == domain), None)

    def type_capability(self, domain: str, type: str) -> TypeCapability | None:
        cap = self.domain(domain)
        return next((t for t in cap.types if t.type == type), None) if cap else None

    def can_read(self, domain: str) -> bool:
        cap = self.domain(domain)
        return bool(cap and any(t.read_fields for t in cap.types))

    def can_write(self, domain: str) -> bool:
        cap = self.domain(domain)
        return bool(cap and any(t.write_fields for t in cap.types))


# --- Bundle manifest ---------------------------------------------------------------------------


class ToolInfo(_Model):
    name: str = "usmp"
    version: str


class ConnectorInfo(_Model):
    connector: Name
    display_name: str
    label: str | None = Field(
        default=None,
        description="Human label for the account or location, e.g. `work calendar`. "
        "Must never contain credentials.",
    )


class TypeCount(_Model):
    domain: Name
    type: Name
    count: int = Field(ge=0)


class BundleManifest(_Model):
    """`manifest.json` at the root of a `.usmp` bundle."""

    format: Literal["usmp-bundle"] = "usmp-bundle"
    format_version: Literal[1] = 1
    created_at: AwareDatetime
    tool: ToolInfo
    source: ConnectorInfo
    capabilities: CapabilityManifest = Field(description="The source connector's capabilities.")
    counts: list[TypeCount]
    items_sha256: Sha256 = Field(description="Hash of the exact bytes of `items.jsonl`.")
    losses: int = Field(ge=0, description="Number of records in `losses.json`.")
    raw_entries: int = Field(ge=0, description="Number of files under `raw/`.")


# --- Verification report -----------------------------------------------------------------------


class ItemStatus(StrEnum):
    VERIFIED = "verified"
    VERIFIED_WITH_DECLARED_LOSS = "verified_with_declared_loss"
    MISMATCH = "mismatch"
    MISSING = "missing"


class ItemResult(_Model):
    usmp_id: UsmpId
    type: Name
    status: ItemStatus
    target_native_id: str | None = None
    compared_fields: FieldList = Field(default_factory=list)
    mismatched_fields: FieldList = Field(default_factory=list)
    declared_loss_fields: FieldList = Field(default_factory=list)


class ReportSummary(_Model):
    total: int = Field(ge=0)
    verified: int = Field(ge=0)
    verified_with_declared_loss: int = Field(ge=0)
    mismatch: int = Field(ge=0)
    missing: int = Field(ge=0)

    @classmethod
    def from_results(cls, results: list[ItemResult]) -> Self:
        def count(status: ItemStatus) -> int:
            return sum(1 for r in results if r.status is status)

        return cls(
            total=len(results),
            verified=count(ItemStatus.VERIFIED),
            verified_with_declared_loss=count(ItemStatus.VERIFIED_WITH_DECLARED_LOSS),
            mismatch=count(ItemStatus.MISMATCH),
            missing=count(ItemStatus.MISSING),
        )


class VerificationReport(_Model):
    """The outcome of a migration: what arrived, what was lost, what failed."""

    report_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    domain: Name
    dry_run: bool
    source: ConnectorInfo
    target: ConnectorInfo
    summary: ReportSummary
    items: list[ItemResult]
    losses: list[LossRecord]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        """True when nothing is mismatched or missing."""
        return self.summary.mismatch == 0 and self.summary.missing == 0

    @model_validator(mode="after")
    def _summary_matches_items(self) -> Self:
        if self.summary != ReportSummary.from_results(self.items):
            raise ValueError("summary does not match items")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        return self


__all__ = [
    "BundleManifest",
    "CapabilityManifest",
    "ConnectorInfo",
    "DomainCapability",
    "Item",
    "ItemResult",
    "ItemStatus",
    "LossReason",
    "LossRecord",
    "ReportSummary",
    "Severity",
    "SourceRef",
    "ToolInfo",
    "TypeCapability",
    "TypeCount",
    "VerificationReport",
]
