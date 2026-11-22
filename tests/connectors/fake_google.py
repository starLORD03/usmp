"""A small in-memory Google Calendar API v3 for tests, served through `responses`."""

import copy
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
import responses

API = "https://www.googleapis.com/calendar/v3"
PAGE = 2  # small pages so pagination is always exercised


class FakeGoogle:
    def __init__(self, account: str = "me@example.com") -> None:
        self.account = account
        self.calendars: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, dict[str, Any]]] = {}
        self.log: list[tuple[str, str]] = []
        self.fail_next: list[tuple[int, str]] = []
        self._n = 0
        self.mock = responses.RequestsMock(assert_all_requests_are_fired=False)
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            self.mock.add_callback(
                method, re.compile(re.escape(API) + r".*"), callback=self._handle
            )

    # --- helpers for tests --------------------------------------------------------------------
    def add_calendar(self, cal_id: str, summary: str, **extra: Any) -> None:
        self.calendars[cal_id] = {"id": cal_id, "summary": summary, "accessRole": "owner", **extra}
        self.events.setdefault(cal_id, {})

    def add_event(self, cal_id: str, event: dict[str, Any]) -> None:
        self.events[cal_id][event["id"]] = event

    def session(self) -> requests.Session:
        return requests.Session()

    def state(self) -> str:
        return json.dumps([self.calendars, self.events], sort_keys=True)

    def __enter__(self) -> "FakeGoogle":
        self.mock.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.mock.stop()
        self.mock.reset()

    # --- request handling ---------------------------------------------------------------------
    def _next_id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def _handle(self, request: Any) -> tuple[int, dict[str, str], str]:
        url = urlparse(request.url)
        path = unquote(url.path.removeprefix("/calendar/v3"))
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        body = json.loads(request.body) if request.body else None
        self.log.append((request.method, path))
        if self.fail_next:
            status, text = self.fail_next.pop(0)
            return status, {}, text
        status, payload = self._route(request.method, path, query, body)
        return status, {"Content-Type": "application/json"}, json.dumps(payload)

    def _page(self, items: list[dict[str, Any]], query: dict[str, str]) -> dict[str, Any]:
        start = int(query.get("pageToken", "0"))
        page: dict[str, Any] = {"items": items[start : start + PAGE]}
        if start + PAGE < len(items):
            page["nextPageToken"] = str(start + PAGE)
        return page

    def _route(self, method: str, path: str, query: dict[str, str], body: Any) -> tuple[int, Any]:
        if method == "GET" and path == "/users/me/calendarList":
            return 200, self._page(list(self.calendars.values()), query)
        if method == "GET" and path == "/calendars/primary":
            return 200, {"id": self.account}
        if method == "POST" and path == "/calendars":
            cal_id = self._next_id("cal") + "@group.calendar.google.com"
            self.add_calendar(
                cal_id, body["summary"], **{k: v for k, v in body.items() if k != "summary"}
            )
            return 200, {"id": cal_id}
        m = re.fullmatch(r"/users/me/calendarList/(.+)", path)
        if m and method == "PATCH":
            self.calendars[m[1]].update(body)
            return 200, self.calendars[m[1]]
        m = re.fullmatch(r"/calendars/([^/]+)/events", path)
        if m and method == "GET":
            return 200, self._page(list(self.events[m[1]].values()), query)
        m = re.fullmatch(r"/calendars/([^/]+)/events/import", path)
        if m and method == "POST":
            return self._import(m[1], body)
        m = re.fullmatch(r"/calendars/([^/]+)/events/([^/]+)_([0-9TZ]+)", path)
        if m and method == "PUT":
            return self._update_instance(m[1], m[2], m[3], body)
        return 404, {"error": f"no route for {method} {path}"}

    def _store(self, cal_id: str, ev: dict[str, Any]) -> dict[str, Any]:
        ev = copy.deepcopy(ev)
        ev.setdefault("status", "confirmed")
        ev.setdefault("organizer", {"email": self.account, "self": True})
        if ev.get("transparency") == "opaque":
            del ev["transparency"]  # Google omits the default
        self.events[cal_id][ev["id"]] = ev
        return ev

    def _import(self, cal_id: str, body: dict[str, Any]) -> tuple[int, Any]:
        for ev in self.events[cal_id].values():
            if ev.get("iCalUID") == body["iCalUID"] and "recurringEventId" not in ev:
                return 409, {
                    "error": {"code": 409, "message": "The requested identifier already exists."}
                }
        return 200, self._store(cal_id, {**body, "id": self._next_id("ev")})

    def _update_instance(
        self, cal_id: str, master: str, suffix: str, body: dict[str, Any]
    ) -> tuple[int, Any]:
        series = self.events[cal_id][master]
        if "T" in suffix:
            when = datetime.strptime(suffix, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ")
            original = {"dateTime": when, "timeZone": series["start"].get("timeZone", "UTC")}
        else:
            original = {"date": f"{suffix[:4]}-{suffix[4:6]}-{suffix[6:]}"}
        ev = {
            **body,
            "id": f"{master}_{suffix}",
            "iCalUID": series["iCalUID"],
            "recurringEventId": master,
            "originalStartTime": original,
        }
        return 200, self._store(cal_id, ev)
