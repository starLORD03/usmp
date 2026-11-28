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
import hashlib
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any, ClassVar
from urllib.parse import unquote, urlparse

from caldav.davclient import DAVClient
from caldav.elements import cdav, dav, ical
from caldav.elements.base import ValuedBaseElement
from caldav.lib import error as caldav_error
from caldav.lib.url import URL

from usmp.connectors._ical import (
    HEX_COLOR,
    build_vcalendar,
    calendar_capabilities,
    event_records,
    marked_events,
    parse_vcalendar,
    recurrence_key,
    unstorable_fields,
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
from usmp.sdk.retry import RetryPolicy, TransientError, parse_retry_after, retrying

NAME = "caldav"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PROPFIND", "REPORT"})
_MULTIGET_BATCH = 100  # events fetched per calendar-multiget request
# Requests that are safe to send again: reads, and writes that put the same thing in the same
# place. New events use a conditional PUT, which the server refuses to apply twice.
_REPEATABLE = _SAFE_METHODS | {"PUT", "PROPPATCH"}
_UNCLEAR_STATUS = frozenset({500, 502, 503, 504})
DEFAULT_WORKERS = 4
MAX_WORKERS = 16


class _UsmpId(ValuedBaseElement):
    """A WebDAV property marking a calendar USMP created with the calendar's ``usmp_id``."""

    tag = "{https://github.com/starLORD03/usmp}usmp-id"


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


class _RetryingClient(DAVClient):
    """A DAV client that retries rate limits, busy servers and dropped connections.

    Any request is retried when the server says it is too busy (429, or 503 with
    ``Retry-After``): nothing was done. Server errors and dropped connections are only retried for
    requests that are safe to repeat (:data:`_REPEATABLE`).
    """

    policy: ClassVar[RetryPolicy | None] = None  # None: the SDK default

    def request(self, url: str, method: str = "GET", *args: Any, **kwargs: Any) -> Any:
        repeatable = method.upper() in _REPEATABLE

        def attempt() -> Any:
            try:
                response = super(_RetryingClient, self).request(url, method, *args, **kwargs)
            except caldav_error.RateLimitError as exc:
                raise TransientError(
                    f"the server is busy (HTTP {exc.status})", retry_after=exc.retry_after_seconds
                ) from exc
            except OSError as exc:
                if not repeatable:
                    raise
                raise TransientError(f"connection problem: {exc}") from exc
            if repeatable and response.status in _UNCLEAR_STATUS:
                wait = parse_retry_after((response.headers or {}).get("Retry-After"))
                raise TransientError(f"server error (HTTP {response.status})", retry_after=wait)
            return response

        return retrying(attempt, what=f"the CalDAV {method} request", policy=self.policy)


class _ReadOnlyClient(_RetryingClient):
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


def _event_losses(item: Item) -> tuple[LossRecord, ...]:
    return tuple(
        LossRecord(
            usmp_id=item.usmp_id,
            field=f,
            reason=LossReason.VALUE_CONVERTED,
            stage="import",
            detail="iCalendar cannot store this value exactly",
        )
        for f in unstorable_fields(item.payload)
    )


def _records_for_resource(obj: Any, parent: str, url: str | None = None) -> Iterator[ExportRecord]:
    url = url or str(obj.url)
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
    def __init__(self, client: DAVClient, only: str | None, workers: int = 1) -> None:
        self._client: Any = client  # the caldav library is only partly typed
        self._only = only
        self._workers = workers
        self._fresh: set[str] = set()  # calendars created by this writer
        self._placed: dict[str, Any] = {}  # calendar usmp_id -> calendar an earlier run made

    def _target_calendars(self) -> dict[str, Any]:
        with _errors("listing calendars"):
            return {_calendar_payload(c)["name"]: c for c in self._client.principal().calendars()}

    def _make(
        self, existing: dict[str, Any], name: str, usmp_id: str | None = None
    ) -> tuple[Any, str]:
        unique, n = name, 1
        while unique in existing:
            n += 1
            unique = f"{name} ({n})"
        with _errors(f"creating calendar {unique!r}"):
            cal = self._client.principal().make_calendar(name=unique)
        existing[unique] = cal
        self._fresh.add(str(cal.url))
        if usmp_id:
            # Lets a re-run recognise the calendar even while it is empty. Optional: servers
            # that refuse custom properties are still recognised by the events inside.
            with contextlib.suppress(caldav_error.DAVError, OSError):
                cal.set_properties([_UsmpId(usmp_id)])
        return cal, unique

    def _save(self, cal: Any, uid: str, data: str, usmp_ids: Sequence[str] = ()) -> str:
        """Store a new resource and return its URL, never replacing an existing event."""
        if str(cal.url) not in self._fresh:
            # An existing calendar: let the library look for the UID first.
            try:
                return str(cal.save_event(data, no_overwrite=True).url)
            except caldav_error.ConsistencyError as exc:
                raise ConnectorError(
                    f"event {uid} already exists in the target calendar; not overwriting it"
                ) from exc
        # A calendar created in this run: one request per event. If-None-Match makes the
        # server refuse to replace anything (RFC 4791 section 5.3.2).
        url = str(cal.url.join(hashlib.sha256(uid.encode()).hexdigest()[:32] + ".ics"))
        headers = {"Content-Type": 'text/calendar; charset="utf-8"', "If-None-Match": "*"}
        response = self._client.put(url, data, headers)
        if response.status == 412:
            if usmp_ids and self._holds(url, usmp_ids):
                # A retried PUT whose first try was stored but whose answer was lost.
                return url
            raise ConnectorError(f"event {uid} already exists in the target calendar")
        if response.status >= 400:
            raise ConnectorError(f"the server refused event {uid} (HTTP {response.status})")
        return url

    def _holds(self, url: str, usmp_ids: Sequence[str]) -> bool:
        """Whether the resource at ``url`` is exactly the events with these ``usmp_ids``."""
        response = self._client.request(url, "GET")
        if response.status >= 400:
            return False
        vcal = parse_vcalendar(response.raw, url)
        stored = {usmp_id for usmp_id, _ in marked_events(vcal, _event_native_id(url))}
        return stored == set(usmp_ids)

    def already_written(
        self, items: Sequence[Item], known: Mapping[str, WriteResult]
    ) -> Iterator[WriteResult]:
        with _errors("looking for items an earlier run wrote"):
            calendars = self._client.principal().calendars()
            marked: dict[str, Any] = {}  # calendar usmp_id -> calendar
            events: dict[str, tuple[Any, str]] = {}  # event usmp_id -> (calendar, native id)
            for cal in calendars:
                props = cal.get_properties([_UsmpId()])
                if props.get(_UsmpId.tag):
                    marked[str(props[_UsmpId.tag])] = cal
                for obj in cal.events():
                    url = str(obj.url)
                    for usmp_id, native_id in marked_events(
                        parse_vcalendar(obj.data, url), _event_native_id(url)
                    ):
                        events[usmp_id] = (cal, native_id)
        by_url = {str(c.url): c for c in calendars}
        # A calendar holding events written from this calendar's events is its earlier copy.
        holding: dict[str, Any] = {}
        for item in items:
            if item.type == "event" and item.usmp_id in events and item.parent:
                holding.setdefault(item.parent, events[item.usmp_id][0])

        for item in items:
            if item.type == "calendar" and self._only is None:
                earlier = known.get(item.usmp_id)
                cal = (
                    marked.get(item.usmp_id)
                    or (by_url.get(earlier.native_id) if earlier else None)
                    or holding.get(item.usmp_id)
                )
                if cal is None:
                    continue
                self._placed[item.usmp_id] = cal
                losses = list(earlier.losses) if earlier else self._present_losses(cal, item)
                yield WriteResult(item.usmp_id, str(cal.url), tuple(losses))
            elif item.type == "event" and item.usmp_id in events:
                yield WriteResult(item.usmp_id, events[item.usmp_id][1], _event_losses(item))

    def _present_losses(self, cal: Any, item: Item) -> list[LossRecord]:
        with _errors("reading a calendar"):
            name = _calendar_payload(cal)["name"]
        losses = []
        if name != item.payload["name"]:
            losses.append(self._loss(item, "name", f"stored as {name!r}"))
        if "time_zone" in item.payload:
            losses.append(
                self._loss(item, "time_zone", "default calendar zone is not set over CalDAV")
            )
        return losses

    def write(self, items: Iterable[Item]) -> Iterator[WriteResult]:
        items = list(items)
        existing = self._target_calendars()
        by_source: dict[str, Any] = dict(self._placed)
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
                cal, name = self._make(existing, str(item.payload["name"]), item.usmp_id)
            if name != item.payload["name"]:
                losses.append(self._loss(item, "name", f"stored as {name!r}"))
            if merged is not None:
                # Merging never changes the existing calendar's own settings.
                losses += [
                    self._loss(item, key, f"merged into the existing calendar {name!r}")
                    for key in ("description", "color", "time_zone")
                    if key in item.payload
                ]
            else:
                losses += self._apply_properties(cal, item)
            by_source[item.usmp_id] = cal
            yield WriteResult(item.usmp_id, str(cal.url), tuple(losses))

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

        jobs = [(calendars[cal_url], uid, group) for (cal_url, uid), group in groups.items()]
        # Existing calendars are written one event at a time: the library first checks the UID.
        for cal, uid, group in jobs:
            if str(cal.url) not in self._fresh:
                yield from self._store(cal, uid, group)
        yield from self._store_parallel([j for j in jobs if str(j[0].url) in self._fresh])

    def _store(self, cal: Any, uid: str, group: Sequence[Item]) -> list[WriteResult]:
        """Save one CalDAV resource (an event and its moved occurrences)."""
        usmp_ids = [i.usmp_id for i in group]
        data = build_vcalendar([i.payload for i in group], usmp_ids=usmp_ids).decode()
        with _errors(f"saving event {uid}"):
            saved_url = self._save(cal, uid, data, usmp_ids)
        results = []
        for item in group:
            rid = recurrence_key(vevent(item.payload)) if "recurrence_id" in item.payload else None
            results.append(
                WriteResult(
                    item.usmp_id, _event_native_id(saved_url)(uid, rid), _event_losses(item)
                )
            )
        return results

    def _store_parallel(
        self, jobs: Sequence[tuple[Any, str, Sequence[Item]]]
    ) -> Iterator[WriteResult]:
        """Save resources in new calendars with up to ``workers`` requests at a time.

        Each result is yielded once its resource is stored. After a failure no new saves start;
        saves already under way finish and are reported before the error is raised.
        """
        if self._workers <= 1 or len(jobs) < 2:
            for job in jobs:
                yield from self._store(*job)
            return
        pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="usmp-caldav")
        try:
            futures: list[Future[list[WriteResult]]] = [pool.submit(self._store, *j) for j in jobs]
            failure: ConnectorError | None = None
            for future in as_completed(futures):
                if future.cancelled():  # pragma: no cover - as_completed skips cancelled ones
                    continue
                try:
                    results = future.result()
                except ConnectorError as exc:
                    if failure is None:
                        failure = exc
                        for other in futures:
                            other.cancel()
                    continue
                yield from results
            if failure is not None:
                raise failure
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

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
        by_calendar: dict[str, dict[str, None]] = defaultdict(dict)
        for native_id in native_ids:
            url = native_id.split("#", 1)[0]
            if url.endswith("/"):
                with _errors("reading a calendar back"):
                    cal = self._client.calendar(url=url)
                    yield calendar_item(NAME, url, _calendar_payload(cal))
            else:
                by_calendar[url.rsplit("/", 1)[0] + "/"][url] = None
        for cal_url, urls in by_calendar.items():
            with _errors("reading events back"):
                cal = self._client.calendar(url=cal_url)
                parent = calendar_item(NAME, cal_url, _calendar_payload(cal))
            resources = list(urls)
            for start in range(0, len(resources), _MULTIGET_BATCH):
                with _errors("reading events back"):
                    # Resources the server no longer has are skipped: they count as missing.
                    batch = resources[start : start + _MULTIGET_BATCH]
                    objects = cal.multiget([URL.objectify(u) for u in batch])
                # Servers may answer with differently escaped URLs (a%40b.ics vs a@b.ics).
                asked = {unquote(u): u for u in batch}
                for obj in objects:
                    resource = asked.get(unquote(str(obj.url)))
                    if resource is None:
                        continue
                    for record in _records_for_resource(obj, parent.usmp_id, resource):
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
        "workers": f"events uploaded at once into new calendars (default {DEFAULT_WORKERS})",
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

    def _workers(self) -> int:
        value = self.config.get("workers", str(DEFAULT_WORKERS)).strip()
        if not value.isdigit() or not 1 <= int(value) <= MAX_WORKERS:
            raise ConnectorError(
                f"{self.display_name}: workers must be a whole number from 1 to {MAX_WORKERS}"
            )
        return int(value)

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
        workers = self._workers()
        return _CalDavWriter(self._client(_RetryingClient), self.config.get("calendar"), workers)
