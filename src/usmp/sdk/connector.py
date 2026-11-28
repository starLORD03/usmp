"""The interfaces every connector implements.

A connector is used in one of two roles:

* **Reader**: exports items from a source. A Reader has no methods that change the provider, and
  the engine only ever opens a source as a Reader. This is what makes migrations non-destructive.
* **Writer**: imports items into a target and reads them back for verification. Writers may also
  remove what a migration created, to undo it (:meth:`Writer.remove`).
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import TracebackType
from typing import ClassVar, Self

from usmp.core.models import CapabilityManifest, ConnectorInfo, Item, LossRecord

ConnectorConfig = Mapping[str, str]


class ConnectorError(Exception):
    """A problem the user can act on: bad configuration, failed sign-in, unsupported operation."""


@dataclass(frozen=True)
class ExportRecord:
    """One item read from a source, with anything the canonical model could not hold."""

    item: Item
    losses: Sequence[LossRecord] = ()
    raw: bytes | None = None
    """The provider's original representation, stored in the bundle under ``raw/``."""
    raw_name: str | None = None

    def __post_init__(self) -> None:
        if (self.raw is None) != (self.raw_name is None):
            raise ValueError("raw and raw_name must be given together")


@dataclass(frozen=True)
class WriteResult:
    """The outcome of writing one item to a target."""

    usmp_id: str
    native_id: str
    """The target's identifier for the written item; used to read it back."""
    losses: Sequence[LossRecord] = field(default_factory=tuple)
    created: bool = False
    """The writer made a new container for this item (e.g. a new calendar) rather than reusing
    one that was already there. Undo only removes containers that a migration created."""


@dataclass(frozen=True)
class Removal:
    """One item a migration created, to be removed when it is undone."""

    usmp_id: str
    native_id: str
    """Where the migration wrote the item, as its :class:`WriteResult` said."""
    type: str
    created: bool = False
    """``WriteResult.created`` as recorded when the item was written."""


class RemovalOutcome(StrEnum):
    REMOVED = "removed"
    ALREADY_GONE = "already_gone"
    KEPT = "kept"


@dataclass(frozen=True)
class RemovalResult:
    usmp_id: str
    outcome: RemovalOutcome
    reason: str | None = None
    """Why the item was kept; shown to the user."""


class _Closeable:
    def close(self) -> None:
        """Release connections or files. Called by the engine when it is done."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class Reader(_Closeable, ABC):
    """Read-only access to a provider. Implementations MUST NOT modify the provider."""

    @abstractmethod
    def read(self, domain: str) -> Iterator[ExportRecord]:
        """Yield every item of ``domain``. Parents (e.g. calendars) before children (events)."""


class Writer(_Closeable, ABC):
    """Write access to a target, plus read-back for verification."""

    def already_written(
        self, items: Sequence[Item], known: Mapping[str, WriteResult]
    ) -> Iterator[WriteResult]:
        """Yield a result for each of ``items`` that is already on the target.

        Called before :meth:`write`, which then only receives the remaining items. ``known`` holds
        what an interrupted run recorded as written, keyed by ``usmp_id``. Writers that store
        ``usmp_id`` on the target should override this to look items up there, so that re-runs
        without a progress record are safe too, and should remember where already-present
        parents are so that :meth:`write` can put new children under them. The default trusts
        ``known``: read-back verification still checks that those items exist.
        """
        for item in items:
            if item.usmp_id in known:
                yield known[item.usmp_id]

    @abstractmethod
    def write(self, items: Iterable[Item]) -> Iterator[WriteResult]:
        """Write ``items`` and yield one result per item, as soon as that item is stored.

        Results may come in any order. Yielding only after an item is stored lets an
        interrupted migration resume without writing it twice.
        """

    @property
    def can_remove(self) -> bool:
        """Whether this writer implements :meth:`remove`, so migrations into it can be undone."""
        return type(self).remove is not Writer.remove

    def remove(self, domain: str, removals: Sequence[Removal]) -> Iterator[RemovalResult]:
        """Remove items a migration created and yield one result per removal.

        The engine only asks for items that this migration wrote and that still match what was
        verified. Implementations MUST additionally:

        * remove an item only if it still carries the USMP marker for ``removal.usmp_id``, and
          otherwise keep it (``KEPT``) with a reason;
        * never remove a container (e.g. a calendar) that still holds anything, nor one they
          cannot show was created by USMP;
        * never remove something that was not asked for: if removing one item would take others
          with it (e.g. a recurring event with a changed occurrence), keep them all;
        * report items that no longer exist as ``ALREADY_GONE``.

        ``removals`` holds children before their containers.
        """
        raise ConnectorError("this target cannot remove items, so the migration cannot be undone")
        yield  # pragma: no cover - makes this a generator

    @abstractmethod
    def read_back(self, domain: str, native_ids: Sequence[str]) -> Iterator[Item]:
        """Yield the current state of the given items, as the target stores them.

        Items that cannot be found are simply not yielded. Each yielded item's
        ``source.native_id`` must be one of ``native_ids``.
        """


class Connector(ABC):
    """A provider plugin. Subclasses are registered under the ``usmp.connectors`` entry point."""

    name: ClassVar[str]
    """Lower-case identifier used on the command line, e.g. ``caldav``."""
    display_name: ClassVar[str]
    description: ClassVar[str] = ""
    config_keys: ClassVar[Mapping[str, str]] = {}
    """Accepted configuration keys and what they mean. ``label`` is always accepted."""

    def __init__(self, config: ConnectorConfig | None = None) -> None:
        self.config: dict[str, str] = dict(config or {})
        unknown = set(self.config) - set(self.config_keys) - {"label"}
        if unknown:
            accepted = ", ".join(sorted({*self.config_keys, "label"}))
            raise ConnectorError(
                f"{self.name}: unknown option(s) {', '.join(sorted(unknown))}; accepted: {accepted}"
            )

    @property
    def label(self) -> str | None:
        return self.config.get("label")

    def require(self, key: str) -> str:
        """Return a required configuration value or raise a helpful error."""
        value = self.config.get(key, "").strip()
        if not value:
            hint = self.config_keys.get(key, "")
            raise ConnectorError(f"{self.name}: missing required option '{key}' ({hint})")
        return value

    def info(self) -> ConnectorInfo:
        return ConnectorInfo(connector=self.name, display_name=self.display_name, label=self.label)

    @abstractmethod
    def capabilities(self) -> CapabilityManifest:
        """What this connector can read and write, given its configuration."""

    def open_reader(self) -> Reader:
        raise ConnectorError(f"{self.display_name} cannot be used as a source")

    def open_writer(self) -> Writer:
        raise ConnectorError(f"{self.display_name} cannot be used as a target")
