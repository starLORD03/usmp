"""The interfaces every connector implements.

A connector is used in one of two roles:

* **Reader**: exports items from a source. A Reader has no methods that change the provider, and
  the engine only ever opens a source as a Reader. This is what makes migrations non-destructive.
* **Writer**: imports items into a target and reads them back for verification.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
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
