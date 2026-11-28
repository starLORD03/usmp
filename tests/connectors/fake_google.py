"""A small in-memory Google Calendar API v3 for tests, served through `responses`."""

import copy
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
import responses

from usmp.sdk.retry import RetryPolicy

API = "https://www.googleapis.com/calendar/v3"
PAGE = 2  # small pages so pagination is always exercised
NO_WAIT = RetryPolicy(sleep=lambda _seconds: None)  # retry at once in tests


class FakeGoogle:
    def __init__(self, account: str = "me@example.com") -> None:
        self.account = account
        self.calendars: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, dict[str, Any]]] = {}
        self.log: list[tuple[str, str]] = []
        self.fail_next: list[tuple[int, str] | tuple[int, str, dict[str, str]]] = []
        # (path suffix, status): carry out the next matching request, then answer with an error,
        # as if the connection dropped after Google stored it.
        self.lose_answers: list[tuple[str, int]] = []
        # (path suffix, status): refuse the next matching write without carrying it out.
        self.refuse: list[tuple[str, int]] = []
        self._n = 0
        self._etags = 0
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
            status, text, *headers = self.fail_next.pop(0)
            return status, headers[0] if headers else {}, text
        for n, (suffix, refused) in enumerate(self.refuse):
            if path.endswith(suffix) and request.method != "GET":
                del self.refuse[n]
                return refused, {}, "backend error"
        if_match = request.headers.get("If-Match")
        status, payload = self._route(request.method, path, query, body, if_match)
        for n, (suffix, lost) in enumerate(self.lose_answers):
            if path.endswith(suffix) and request.method != "GET":
                del self.lose_answers[n]
                return lost, {}, "backend error"
        if payload is None:
            return status, {}, ""
        return status, {"Content-Type": "application/json"}, json.dumps(payload)

    def _page(self, items: list[dict[str, Any]], query: dict[str, str]) -> dict[str, Any]:
        start = int(query.get("pageToken", "0"))
        page: dict[str, Any] = {"items": items[start : start + PAGE]}
        if start + PAGE < len(items):
            page["nextPageToken"] = str(start + PAGE)
        return page

    def edit_event(self, cal_id: str, event_id: str, **changes: Any) -> None:
        """Change an event as its owner would in Google Calendar (a new ETag)."""
        self._store(cal_id, {**self.events[cal_id][event_id], **changes})

    def _route(
        self, method: str, path: str, query: dict[str, str], body: Any, if_match: str | None = None
    ) -> tuple[int, Any]:
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
        m = re.fullmatch(r"/calendars/([^/]+)", path)
        if m and method == "DELETE":
            if m[1] not in self.calendars:
                return 404, {"error": "not found"}
            del self.calendars[m[1]], self.events[m[1]]
            return 204, None
        m = re.fullmatch(r"/calendars/([^/]+)/events", path)
        if m and method == "GET":
            if m[1] not in self.events:
                return 404, {"error": "not found"}
            found = [
                ev for ev in self.events[m[1]].values()
                if query.get("iCalUID") in (None, ev.get("iCalUID"))
            ]  # fmt: skip
            if "maxResults" in query:
                found = found[: int(query["maxResults"])]
            return 200, self._page(found, query)
        m = re.fullmatch(r"/calendars/([^/]+)/events/import", path)
        if m and method == "POST":
            return self._import(m[1], self._like_google(body, query))
        m = re.fullmatch(r"/calendars/([^/]+)/events/([^/]+)", path)
        if m and method in ("GET", "DELETE") and m[2] != "import":
            ev = self.events.get(m[1], {}).get(m[2])
            if ev is None:
                return 404, {"error": "not found"}
            if method == "GET":
                return 200, ev
            if if_match and if_match != ev.get("etag"):
                return 412, {"error": "precondition failed"}
            for other in list(self.events[m[1]].values()):
                if other["id"] == m[2] or other.get("recurringEventId") == m[2]:
                    del self.events[m[1]][other["id"]]
            return 204, None
        m = re.fullmatch(r"/calendars/([^/]+)/events/([^/]+)_([0-9TZ]+)", path)
        if m and method == "PUT":
            return self._update_instance(m[1], m[2], m[3], self._like_google(body, query))
        return 404, {"error": f"no route for {method} {path}"}

    @staticmethod
    def _like_google(body: dict[str, Any], query: dict[str, str]) -> dict[str, Any]:
        """What Google keeps of a written event."""
        body = dict(body)
        if query.get("conferenceDataVersion") != "1":
            body.pop("conferenceData", None)  # ignored unless the client opts in
        if (
            "conferenceData" in body
            and body["conferenceData"].get("conferenceSolution", {}).get("key", {}).get("type")
            == "hangoutsMeet"
        ):
            video = [
                e for e in body["conferenceData"]["entryPoints"] if e["entryPointType"] == "video"
            ]
            body["hangoutLink"] = video[0]["uri"]
        body.pop("eventType", None)  # import only creates ordinary events
        rooms = [a.get("displayName") or a["email"] for a in body.get("attendees", [])
                 if a.get("resource")]  # fmt: skip
        if rooms:  # Google adds booked rooms to the location
            body["location"] = ", ".join(
                [body["location"], *rooms] if body.get("location") else rooms
            )
        return body

    def _store(self, cal_id: str, ev: dict[str, Any]) -> dict[str, Any]:
        ev = copy.deepcopy(ev)
        ev.setdefault("status", "confirmed")
        ev.setdefault("organizer", {"email": cal_id, "self": True})
        self._etags += 1
        ev["etag"] = f'"{self._etags}"'
        if ev.get("transparency") == "opaque":
            del ev["transparency"]  # Google omits the default
        self.events[cal_id][ev["id"]] = ev
        return ev

    def _import(self, cal_id: str, body: dict[str, Any]) -> tuple[int, Any]:
        organizer = (body.get("organizer") or {}).get("email")
        guests = {a.get("email") for a in body.get("attendees", [])}
        if organizer and cal_id not in guests | {organizer}:
            reason = "participantIsNeitherOrganizerNorAttendee"
            return 400, {"error": {"code": 400, "errors": [{"reason": reason}]}}
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
