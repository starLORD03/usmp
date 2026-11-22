"""CalDAV calendars: iCloud, Nextcloud, Fastmail, Radicale and other CalDAV servers.

Configuration::

    caldav:url=https://caldav.icloud.com,username=me@icloud.com

The password is never given on the command line. It is read from the environment variable named
by ``password_env``, or from the OS keychain (store it with
``usmp auth set caldav <username>@<url>``). For iCloud, use an app-specific password.

As a source, the connector talks to the server through a client that refuses every request method
that could change data. As a target, it creates new calendars (or uses the one named by
``calendar``) and never overwrites existing events.
"""

import contextlib
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, ClassVar
from urllib.parse import unquote, urlparse

from caldav.calendarobjectresource import Event as DavEvent
from caldav.davclient import DAVClient
from caldav.elements import cdav, dav, ical
from caldav.lib import error as caldav_error

from usmp.connectors._ical import (
    HEX_COLOR,
    build_vcalendar,
    calendar_capabilities,
    event_records,
    parse_vcalendar,
    recurrence_key,
    vevent,
)
from usmp.core.models import CapabilityManifest, Item, LossReason, LossRecord, Severity
from usmp.domains.calendar import calendar_item
from usmp.sdk import credentials
from usmp.sdk.connector import (
    Connector,
    ConnectorError,
    ExportRecord,
    Reader,
    Writer,
    WriteResult,
)

NAME = "caldav"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PROPFIND", "REPORT"})


@contextlib.contextmanager
def _errors(action: str) -> Iterator[None]:
    """Turn library and network errors into messages a user can act on."""
    try:
        yield
    except ConnectorError:
        raise
    except caldav_error.AuthorizationError as exc:
        raise ConnectorError(
            f"sign-in failed while {action}: check the username and password "
            "(iCloud needs an app-specific password)"
        ) from exc
    except caldav_error.NotFoundError as exc:
        raise ConnectorError(f"not found while {action}: check the URL") from exc
    except (caldav_error.DAVError, OSError) as exc:
        raise ConnectorError(f"CalDAV error while {action}: {exc}") from exc


class _ReadOnlyClient(DAVClient):
    """A DAV client that can only read. Used for sources."""

    def request(self, url: str, method: str = "GET", *args: Any, **kwargs: Any) -> Any:
        if method.upper() not in _SAFE_METHODS:
            raise ConnectorError(f"blocked a {method} request: this source is read-only")
        return super().request(url, method, *args, **kwargs)


def _calendar_payload(cal: Any) -> dict[str, Any]:
    props = cal.get_properties(
        [dav.DisplayName(), ical.CalendarColor(), cdav.CalendarDescription()]
    )
    name = props.get(dav.DisplayName.tag) or unquote(str(cal.url).rstrip("/").rsplit("/", 1)[-1])
    payload: dict[str, Any] = {"name": name}
    color = props.get(ical.CalendarColor.tag) or ""
    if HEX_COLOR.fullmatch(color):
        payload["color"] = color[:7]
    description = props.get(cdav.CalendarDescription.tag)
    if description:
        payload["description"] = description
    return payload


def _event_native_id(resource_url: str) -> Any:
    def native_id(uid: str, rid: str | None) -> str:
        return resource_url + (f"#{rid}" if rid else "")

    return native_id


def _records_for_resource(obj: Any, parent: str) -> Iterator[ExportRecord]:
    url = str(obj.url)
    vcal = parse_vcalendar(obj.data, url)
    yield from event_records(
        vcal, connector=NAME, parent=parent, native_id_for=_event_native_id(url), where=url
    )


class _CalDavReader(Reader):
    def __init__(self, client: DAVClient, only: str | None) -> None:
        self._client: Any = client  # the caldav library is only partly typed
        self._only = only

    def read(self, domain: str) -> Iterator[ExportRecord]:
        if domain != "calendar":
            return
        with _errors("listing calendars"):
            # Servers list calendars in no particular order; keep exports deterministic.
            calendars = sorted(self._client.principal().calendars(), key=lambda c: str(c.url))
        found = []
        for cal in calendars:
            with _errors("reading a calendar"):
                payload = _calendar_payload(cal)
            found.append(payload["name"])
            if self._only and payload["name"] != self._only:
                continue
            cal_item = calendar_item(NAME, str(cal.url), payload)
            yield ExportRecord(item=cal_item)
            with _errors(f"reading events of {payload['name']!r}"):
                objects = cal.events()
            for obj in objects:
                yield from _records_for_resource(obj, cal_item.usmp_id)
        if self._only and self._only not in found:
            raise ConnectorError(
                f"no calendar named {self._only!r}; available: {', '.join(sorted(found)) or 'none'}"
            )

    def close(self) -> None:
        self._client.close()


class _CalDavWriter(Writer):
    def __init__(self, client: DAVClient, only: str | None) -> None:
        self._client: Any = client  # the caldav library is only partly typed
        self._only = only

    def _target_calendars(self) -> dict[str, Any]:
        with _errors("listing calendars"):
            return {_calendar_payload(c)["name"]: c for c in self._client.principal().calendars()}

    def _make(self, existing: dict[str, Any], name: str) -> tuple[Any, str]:
        unique, n = name, 1
        while unique in existing:
            n += 1
            unique = f"{name} ({n})"
        with _errors(f"creating calendar {unique!r}"):
            cal = self._client.principal().make_calendar(name=unique)
        existing[unique] = cal
        return cal, unique

    def write(self, items: Iterable[Item]) -> Iterator[WriteResult]:
        items = list(items)
        existing = self._target_calendars()
        by_source: dict[str, Any] = {}
        results: dict[str, WriteResult] = {}
        merged: Any = None
        if self._only:
            merged = existing.get(self._only)
            if merged is None:
                merged, _ = self._make(existing, self._only)

        for item in items:
            if item.domain != "calendar" or item.type not in ("calendar", "event"):
                raise ConnectorError(f"cannot write {item.domain}/{item.type} items to CalDAV")
            if item.type != "calendar":
                continue
            losses: list[LossRecord] = []
            if merged is not None:
                cal, name = merged, self._only
            else:
                cal, name = self._make(existing, str(item.payload["name"]))
            if name != item.payload["name"]:
                losses.append(self._loss(item, "name", f"stored as {name!r}"))
            losses += self._apply_properties(cal, item)
            by_source[item.usmp_id] = cal
            results[item.usmp_id] = WriteResult(item.usmp_id, str(cal.url), tuple(losses))

        # One CalDAV resource per UID: the master event plus its overridden occurrences.
        groups: dict[tuple[str, str], list[Item]] = defaultdict(list)
        calendars: dict[str, Any] = {}
        for item in items:
            if item.type != "event":
                continue
            cal = by_source.get(item.parent or "") or merged
            if cal is None:
                cal, _ = self._make(existing, "Imported")
                merged = cal
            calendars[str(cal.url)] = cal
            groups[(str(cal.url), str(item.payload["uid"]))].append(item)

        for (cal_url, uid), group in groups.items():
            data = build_vcalendar([i.payload for i in group]).decode()
            with _errors(f"saving event {uid}"):
                try:
                    saved = calendars[cal_url].save_event(data, no_overwrite=True)
                except caldav_error.ConsistencyError as exc:
                    raise ConnectorError(
                        f"event {uid} already exists in the target calendar; not overwriting it"
                    ) from exc
            for item in group:
                rid = (
                    recurrence_key(vevent(item.payload))
                    if "recurrence_id" in item.payload
                    else None
                )
                results[item.usmp_id] = WriteResult(
                    item.usmp_id, _event_native_id(str(saved.url))(uid, rid)
                )

        for item in items:
            yield results[item.usmp_id]

    def _apply_properties(self, cal: Any, item: Item) -> list[LossRecord]:
        losses = []
        props: list[Any] = []
        if "color" in item.payload:
            props.append(ical.CalendarColor(str(item.payload["color"])))
        if "description" in item.payload:
            props.append(cdav.CalendarDescription(str(item.payload["description"])))
        if props:
            try:
                cal.set_properties(props)
            except (caldav_error.DAVError, OSError) as exc:
                for name in ("color", "description"):
                    if name in item.payload:
                        losses.append(self._loss(item, name, f"server refused it: {exc}"))
        if "time_zone" in item.payload:
            losses.append(
                self._loss(item, "time_zone", "default calendar zone is not set over CalDAV")
            )
        return losses

    @staticmethod
    def _loss(item: Item, field: str, detail: str) -> LossRecord:
        return LossRecord(
            usmp_id=item.usmp_id,
            field=field,
            reason=LossReason.VALUE_CONVERTED
            if field == "name"
            else LossReason.UNSUPPORTED_BY_TARGET,
            severity=Severity.INFO if field == "name" else Severity.WARNING,
            stage="import",
            detail=detail,
        )

    def read_back(self, domain: str, native_ids: Sequence[str]) -> Iterator[Item]:
        wanted = set(native_ids)
        resources: dict[str, None] = {}
        for native_id in native_ids:
            url = native_id.split("#", 1)[0]
            if url.endswith("/"):
                with _errors("reading a calendar back"):
                    cal = self._client.calendar(url=url)
                    yield calendar_item(NAME, url, _calendar_payload(cal))
            else:
                resources[url] = None
        for url in resources:
            parent_url = url.rsplit("/", 1)[0] + "/"
            with _errors("reading an event back"):
                obj = DavEvent(self._client, url=url)
                obj.load()
                parent = calendar_item(
                    NAME, parent_url, _calendar_payload(self._client.calendar(url=parent_url))
                )
            for record in _records_for_resource(obj, parent.usmp_id):
                if record.item.source.native_id in wanted:
                    yield record.item

    def close(self) -> None:
        self._client.close()


class CalDavConnector(Connector):
    name = NAME
    display_name = "CalDAV"
    description = "CalDAV servers: iCloud, Nextcloud, Fastmail, Radicale, and others."
    config_keys: ClassVar[Mapping[str, str]] = {
        "url": "server URL, e.g. https://caldav.icloud.com",
        "username": "account name, usually your email address",
        "calendar": "only read this calendar / write everything into this calendar",
        "password_env": "environment variable that holds the password (else the OS keychain)",
    }

    @property
    def account(self) -> str:
        return f"{self.require('username')}@{self.require('url')}"

    def capabilities(self) -> CapabilityManifest:
        return calendar_capabilities(NAME, self.display_name)

    def _password(self) -> str:
        password = credentials.get_secret(NAME, self.account, self.config.get("password_env"))
        if not password:
            raise ConnectorError(
                f"no password for {self.account}: run `usmp auth set caldav {self.account}` "
                "or set password_env to an environment variable holding it"
            )
        return password

    def _client(self, cls: type[DAVClient]) -> DAVClient:
        url = self.require("url")
        if urlparse(url).scheme not in ("http", "https"):
            raise ConnectorError(f"{self.display_name}: url must start with https://")
        return cls(url=url, username=self.require("username"), password=self._password())

    def info(self) -> Any:
        info = super().info()
        if info.label is None:
            info = info.model_copy(update={"label": self.config.get("username")})
        return info

    def open_reader(self) -> Reader:
        return _CalDavReader(self._client(_ReadOnlyClient), self.config.get("calendar"))

    def open_writer(self) -> Writer:
        return _CalDavWriter(self._client(DAVClient), self.config.get("calendar"))
