import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.connectors.fake_google import NO_WAIT, FakeGoogle
from usmp.cli.app import app
from usmp.connectors import gcal
from usmp.connectors._ical import build_vcalendar
from usmp.connectors.gcal import GoogleCalendarConnector, _Api, _minutes_before
from usmp.connectors.ics import IcsConnector
from usmp.core.engine import migrate
from usmp.core.models import Item, ItemStatus
from usmp.domains.calendar import calendar_item, event_item
from usmp.sdk.connector import ConnectorError
from usmp.sdk.retry import RetryPolicy
from usmp.testing import ConnectorConformance

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ics"


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gcal._Api, "policy", NO_WAIT)


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGoogle]:
    fake = FakeGoogle()
    monkeypatch.setattr(
        GoogleCalendarConnector, "session_for", staticmethod(lambda account, write: fake.session)
    )
    with fake:
        yield fake


def connector(**extra: str) -> GoogleCalendarConnector:
    return GoogleCalendarConnector({"account": "me@example.com", **extra})


MEET = {
    "conferenceId": "abc-defg-hij",
    "conferenceSolution": {"key": {"type": "hangoutsMeet"}, "name": "Google Meet"},
    "entryPoints": [
        {
            "entryPointType": "video",
            "uri": "https://meet.google.com/abc-defg-hij",
            "label": "meet.google.com/abc-defg-hij",
        },
        {
            "entryPointType": "phone",
            "uri": "tel:+1-555-0100",
            "label": "+1 555-0100",
            "pin": "123456",
        },
        {"entryPointType": "more", "uri": "https://tel.meet/abc-defg-hij"},
    ],
}


def seed(fake: FakeGoogle) -> None:
    fake.add_calendar(
        "me@example.com",
        "Me",
        timeZone="Europe/Paris",
        backgroundColor="#9FE1E7",
        description="Personal",
        defaultReminders=[{"method": "popup", "minutes": 30}, {"method": "email", "minutes": 1440}],
    )
    fake.add_calendar("holidays@group", "Holidays", accessRole="freeBusyReader")
    events: list[dict[str, Any]] = [
        {
            "id": "series",
            "iCalUID": "series@google.com",
            "summary": "Standup",
            "start": {"dateTime": "2026-01-05T09:00:00+01:00", "timeZone": "Europe/Paris"},
            "end": {"dateTime": "2026-01-05T09:15:00+01:00", "timeZone": "Europe/Paris"},
            "recurrence": [
                "RRULE:FREQ=WEEKLY;BYDAY=MO",
                "EXDATE;TZID=Europe/Paris:20260112T090000",
            ],
            "attendees": [
                {
                    "email": "Amy@x.com",
                    "displayName": "Amy",
                    "responseStatus": "accepted",
                    "optional": True,
                },
                {"email": "room@resource.calendar.google.com", "resource": True},
            ],
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 10}]},
            "conferenceData": MEET,
            "hangoutLink": "https://meet.google.com/abc-defg-hij",
            "colorId": "5",
            "extendedProperties": {"private": {"app": "1"}},
        },
        {
            "id": "series_20260119T080000Z",
            "iCalUID": "series@google.com",
            "summary": "Standup",
            "status": "cancelled",
            "recurringEventId": "series",
            "originalStartTime": {
                "dateTime": "2026-01-19T09:00:00+01:00",
                "timeZone": "Europe/Paris",
            },
        },
        {
            "id": "series_20260126T080000Z",
            "iCalUID": "series@google.com",
            "summary": "Standup (late)",
            "recurringEventId": "series",
            "originalStartTime": {
                "dateTime": "2026-01-26T09:00:00+01:00",
                "timeZone": "Europe/Paris",
            },
            "start": {"dateTime": "2026-01-26T10:00:00+01:00", "timeZone": "Europe/Paris"},
            "end": {"dateTime": "2026-01-26T10:15:00+01:00", "timeZone": "Europe/Paris"},
        },
        {
            "id": "trip",
            "iCalUID": "trip@google.com",
            "summary": "Trip",
            "start": {"date": "2026-02-10"},
            "end": {"date": "2026-02-13"},
            "transparency": "transparent",
            "reminders": {"useDefault": True},
            "eventType": "outOfOffice",
            "outOfOfficeProperties": {"autoDeclineMode": "declineNone"},
            "visibility": "private",
        },
        {
            "id": "call",
            "iCalUID": "call@google.com",
            "summary": "Call",
            "start": {"dateTime": "2026-03-01T15:00:00Z"},
            "end": {"dateTime": "2026-03-01T16:00:00Z"},
        },
    ]
    for ev in events:
        fake.add_event("me@example.com", ev)


def by_uid(records: list[Any]) -> dict[str, dict[str, Any]]:
    return {
        f"{r.item.payload['uid']}|{r.item.payload.get('recurrence_id', '')}": r.item.payload
        for r in records
        if r.item.type == "event"
    }


class TestReading:
    def test_maps_series_exceptions_and_cancellations(self, google: FakeGoogle) -> None:
        seed(google)
        records = list(connector().open_reader().read("calendar"))
        events = by_uid(records)
        series = events["series@google.com|"]
        moved = events["series@google.com|2026-01-26T09:00:00"]

        cals = [r.item.payload for r in records if r.item.type == "calendar"]
        assert cals == [
            {
                "name": "Me",
                "description": "Personal",
                "color": "#9fe1e7",
                "time_zone": "Europe/Paris",
            }
        ], "free/busy-only calendars are skipped"
        assert series["start"] == "2026-01-05T09:00:00"
        assert series["time_zone"] == "Europe/Paris"
        assert series["excluded_dates"] == ["2026-01-12T09:00:00", "2026-01-19T09:00:00"]
        assert series["alarms"] == [{"trigger": "-PT10M"}]
        assert series["attendees"][0] == {
            "email": "amy@x.com",
            "name": "Amy",
            "role": "optional",
            "status": "accepted",
        }
        assert moved["start"] == "2026-01-26T10:00:00"
        assert events["trip@google.com|"]["start"] == "2026-02-10"
        assert events["trip@google.com|"]["availability"] == "free"
        assert events["trip@google.com|"]["privacy"] == "private"
        assert events["trip@google.com|"]["alarms"] == [
            {"trigger": "-P1D", "action": "email"},
            {"trigger": "-PT30M"},
        ], "'use default reminders' copies the calendar's defaults"
        assert events["call@google.com|"]["time_zone"] == "Europe/Paris", "calendar zone applies"
        assert events["call@google.com|"]["start"] == "2026-03-01T16:00:00"

        assert series["attendees"][1]["kind"] == "resource", "rooms stay rooms"
        assert series["conferences"] == [
            {"uri": "https://meet.google.com/abc-defg-hij",
             "label": "meet.google.com/abc-defg-hij", "features": ["video"]},
            {"uri": "https://tel.meet/abc-defg-hij"},
            {"uri": "tel:+1-555-0100", "label": "+1 555-0100 PIN: 123456",
             "features": ["phone"]},
        ]  # fmt: skip
        assert series["provider_data"] == {
            "google": {
                "colorId": "5",
                "conferenceData": MEET,
                "extendedProperties": {"private": {"app": "1"}},
            }
        }
        assert events["trip@google.com|"]["provider_data"]["google"]["eventType"] == "outOfOffice"
        assert "provider_data" not in moved, "nothing Google-only on the moved occurrence"

        assert [loss for r in records for loss in r.losses] == [], "nothing is left behind"
        assert all(r.raw for r in records if r.item.type == "event")

    def test_source_session_only_reads(self, google: FakeGoogle) -> None:
        seed(google)
        reader = connector().open_reader()
        list(reader.read("calendar"))

        assert {m for m, _ in google.log} == {"GET"}
        with pytest.raises(ConnectorError, match="blocked a POST request"):
            reader._api.call("POST", "/calendars")  # type: ignore[attr-defined]

    def test_calendar_filter(self, google: FakeGoogle) -> None:
        seed(google)
        google.add_calendar("work@group", "Work")

        names = [
            r.item.payload["name"]
            for r in connector(calendar="Work").open_reader().read("calendar")
        ]

        assert names == ["Work"]
        with pytest.raises(ConnectorError, match="no calendar named 'Nope'"):
            list(connector(calendar="Nope").open_reader().read("calendar"))
        assert list(connector().open_reader().read("contacts")) == []


def sample() -> list[Item]:
    cal = calendar_item(
        "seed", "c", {"name": "Team", "color": "#33b679", "time_zone": "America/New_York"}
    )
    master = {
        "uid": "s@x",
        "title": "Sync",
        "start": "2026-05-04T09:00:00",
        "end": "2026-05-04T10:00:00",
        "time_zone": "America/New_York",
        "recurrence_rules": ["FREQ=WEEKLY"],
        "excluded_dates": ["2026-05-11T09:00:00"],
        "attendees": [{"email": "a@x.com", "status": "accepted"}],
        "alarms": [{"trigger": "-PT1H"}],
        "organizer": {"email": "boss@x.com"},
        "availability": "busy",
        "status": "confirmed",
    }
    moved = {
        "uid": "s@x",
        "title": "Sync (moved)",
        "start": "2026-05-18T11:00:00",
        "end": "2026-05-18T12:00:00",
        "time_zone": "America/New_York",
        "recurrence_id": "2026-05-18T09:00:00",
    }
    day = {
        "uid": "d@x",
        "title": "Offsite",
        "start": "2026-06-01",
        "end": "2026-06-03",
        "privacy": "public",
    }
    return [
        cal,
        event_item("seed", "1", master, parent=cal.usmp_id),
        event_item("seed", "2", moved, parent=cal.usmp_id),
        event_item("seed", "3", day, parent=cal.usmp_id),
    ]


class TestGoogleConformance(ConnectorConformance):
    finds_written = True
    domain = "calendar"

    @pytest.fixture(autouse=True)
    def _fake(self, google: FakeGoogle) -> None:
        self.fake = google

    def make_connector(self) -> GoogleCalendarConnector:
        return connector()

    def sample_items(self) -> list[Item]:
        return sample()

    def provider_state(self) -> Any:
        return self.fake.state()


class TestWriting:
    def test_round_trip_through_engine(self, google: FakeGoogle, tmp_path: Path) -> None:
        from usmp.connectors.memory import MemoryConnector

        source = MemoryConnector({"domain": "calendar"}, items=sample())
        result = migrate(source, connector(), bundle_path=tmp_path / "b.usmp")

        assert result.report is not None and result.report.ok, result.report
        assert result.report.summary.mismatch == 0
        assert any(path.endswith("_20260518T130000Z") for m, path in google.log if m == "PUT")

    def test_invitation_from_someone_else_adds_the_calendar_as_guest(
        self, google: FakeGoogle, tmp_path: Path
    ) -> None:
        from usmp.connectors.memory import MemoryConnector

        payload = {
            "uid": "inv@x",
            "start": "2026-01-01T09:00:00",
            "end": "2026-01-01T10:00:00",
            "time_zone": "UTC",
            "organizer": {"email": "old-me@example.com"},
        }
        ev = event_item("s", "1", payload)
        result = migrate(
            MemoryConnector({"domain": "calendar"}, items=[ev]),
            connector(),
            bundle_path=tmp_path / "b.usmp",
        )

        assert result.report is not None and result.report.ok, result.report
        assert result.report.summary.verified == 1, "no loss, no mismatch"
        [(cal_id, events)] = [(c, e) for c, e in google.events.items() if e]
        [stored] = events.values()
        assert stored["attendees"] == [{"email": cal_id, "responseStatus": "accepted"}]
        [record] = list(connector().open_reader().read("calendar"))[1:]
        assert "attendees" not in record.item.payload
        assert "provider_data" not in record.item.payload

    def test_unsupported_values_are_declared(self, google: FakeGoogle, tmp_path: Path) -> None:
        from usmp.connectors.memory import MemoryConnector

        ev = event_item(
            "s",
            "1",
            {
                "uid": "x@x",
                "start": "2026-01-01T09:00:00",
                "time_zone": "UTC",
                "attendees": [{"email": "c@x.com", "role": "chair"}],
                "alarms": [{"trigger": "PT5M"}, {"trigger": "-PT5M", "action": "audio"}],
                "attachments": [{"uri": "https://files.example/a.pdf"}],
            },
        )
        result = migrate(
            MemoryConnector({"domain": "calendar"}, items=[ev]),
            connector(),
            bundle_path=tmp_path / "b.usmp",
        )

        assert result.report is not None and result.report.ok, result.report
        [item] = result.report.items
        assert item.status is ItemStatus.VERIFIED_WITH_DECLARED_LOSS
        assert {"attendees", "alarms", "attachments"} <= set(item.declared_loss_fields)

    def test_name_clash_merge_and_rerun(self, google: FakeGoogle, tmp_path: Path) -> None:
        seed(google)
        cal = calendar_item("s", "c", {"name": "Me"})
        [result] = list(connector().open_writer().write([cal]))
        assert google.calendars[result.native_id]["summary"] == "Me (2)"
        assert result.losses[0].detail == "stored as 'Me (2)'"

        ics = IcsConnector({"path": str(FIXTURES / "google.ics")})
        first = migrate(ics, connector(calendar="Imported stuff"), bundle_path=tmp_path / "a.usmp")
        again = migrate(ics, connector(calendar="Imported stuff"), bundle_path=tmp_path / "b.usmp")

        assert first.report is not None and first.report.ok, first.report
        assert again.report is not None and again.report.ok, again.report
        events = [r for r in again.report.items if r.type == "event"]
        assert events and all(r.already_present for r in events)
        assert sum(c["summary"] == "Imported stuff" for c in google.calendars.values()) == 1

    def test_unmarked_event_with_same_uid_is_never_overwritten(
        self, google: FakeGoogle, tmp_path: Path
    ) -> None:
        google.add_calendar("mine", "Mine")
        google.add_event("mine", {"id": "x", "iCalUID": "o@x", "start": {"date": "2026-01-01"}})
        ics = tmp_path / "one.ics"
        ics.write_bytes(
            build_vcalendar([{"uid": "o@x", "start": "2026-01-01", "title": "Mine too"}])
        )

        result = migrate(
            IcsConnector({"path": str(ics)}),
            connector(calendar="Mine"),
            bundle_path=tmp_path / "a.usmp",
        )

        assert result.report is not None and not result.report.ok
        assert any("already exists" in e for e in result.report.errors)
        assert "summary" not in google.events["mine"]["x"]

    def test_orphan_events_and_bad_items(self, google: FakeGoogle) -> None:
        orphan = event_item("s", "1", {"uid": "o@x", "start": "2026-01-01"})
        [result] = list(connector().open_writer().write([orphan]))
        assert google.calendars[result.native_id.split("/")[0]]["summary"] == "Imported"

        lonely = event_item(
            "s", "2", {"uid": "l@x", "start": "2026-01-02", "recurrence_id": "2026-01-01"}
        )
        with pytest.raises(ConnectorError, match="has no series"):
            list(connector().open_writer().write([lonely]))
        note = Item.create(
            connector="s", native_id="n", domain="notes", type="note", schema_version=1, payload={}
        )
        with pytest.raises(ConnectorError, match="cannot write notes/note"):
            list(connector().open_writer().write([note]))


def test_outlook_export_to_google_and_back(google: FakeGoogle, tmp_path: Path) -> None:
    there = migrate(
        IcsConnector({"path": str(FIXTURES / "outlook.ics")}),
        connector(),
        bundle_path=tmp_path / "a.usmp",
    )
    back = migrate(
        connector(), IcsConnector({"path": str(tmp_path / "back")}), bundle_path=tmp_path / "b.usmp"
    )

    assert there.report is not None and there.report.ok, there.report
    assert back.report is not None and back.report.ok, back.report


def test_google_details_survive_a_trip_through_ics(google: FakeGoogle, tmp_path: Path) -> None:
    seed(google)
    out = migrate(
        connector(), IcsConnector({"path": str(tmp_path / "ics")}), bundle_path=tmp_path / "a.usmp"
    )
    assert out.report is not None and out.report.ok, out.report
    assert out.report.summary.verified_with_declared_loss == 0, out.report
    [ics_file] = (tmp_path / "ics").glob("*.ics")
    text = ics_file.read_text()
    assert "CONFERENCE;FEATURE=VIDEO" in text
    assert "Join online: https://meet.google.com/abc-defg-hij" in text, "visible in any app"

    back = migrate(
        IcsConnector({"path": str(tmp_path / "ics")}), connector(), bundle_path=tmp_path / "b.usmp"
    )
    assert back.report is not None and back.report.ok, back.report
    declared = {
        i.usmp_id: i.declared_loss_fields for i in back.report.items if i.declared_loss_fields
    }
    assert sorted(declared.values()) == [["name"], ["provider_data"]], (
        "only the calendar name ('Me (2)') and the out-of-office type change"
    )
    restored = next(
        ev for cal, evs in google.events.items() if cal != "me@example.com"
        for ev in evs.values() if ev.get("iCalUID") == "series@google.com" and "recurrence" in ev
    )  # fmt: skip
    assert restored["conferenceData"]["conferenceId"] == "abc-defg-hij", "same Meet link"
    assert restored["colorId"] == "5"
    assert restored["attendees"][1]["resource"] is True


def test_links_google_cannot_attach_go_into_the_notes(google: FakeGoogle, tmp_path: Path) -> None:
    from usmp.connectors.memory import MemoryConnector

    ev = event_item(
        "s",
        "1",
        {
            "uid": "z@x",
            "start": "2026-01-01T09:00:00",
            "time_zone": "UTC",
            "description": "Agenda",
            "conferences": [{"uri": "https://zoom.example/j/1", "features": ["video"]}],
            "attendees": [{"email": "g@x.com", "kind": "group"}],
            "provider_data": {"outlook": {"x": 1}},
        },
    )
    result = migrate(
        MemoryConnector({"domain": "calendar"}, items=[ev]), connector(), bundle_path=tmp_path / "b"
    )
    assert result.report is not None and result.report.ok, result.report
    [item] = result.report.items
    assert set(item.declared_loss_fields) == {
        "attendees", "conferences", "description", "provider_data"
    }  # fmt: skip
    [stored] = [e for evs in google.events.values() for e in evs.values()]
    assert stored["description"] == "Agenda\n\nJoin online: https://zoom.example/j/1"


class TestApi:
    def test_retries_then_succeeds(self, google: FakeGoogle) -> None:
        seed(google)
        google.fail_next = [(503, "busy"), (403, '{"reason": "rateLimitExceeded"}')]

        assert len(list(connector().open_reader().read("calendar"))) > 1

    @pytest.mark.parametrize(
        ("status", "text", "message"),
        [
            (401, "unauthorized", "sign-in expired"),
            (403, "insufficientPermissions", "read-only"),
            (400, "bad", "Google Calendar error 400"),
        ],
    )
    def test_errors_are_explained(
        self, google: FakeGoogle, status: int, text: str, message: str
    ) -> None:
        google.fail_next = [(status, text)]
        with pytest.raises(ConnectorError, match=message):
            list(connector().open_reader().read("calendar"))

    def test_rate_limits_and_retry_after(
        self, google: FakeGoogle, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slept: list[float] = []
        monkeypatch.setattr(gcal._Api, "policy", RetryPolicy(sleep=slept.append, base=0.01))
        seed(google)
        google.fail_next = [
            (429, "slow down", {"Retry-After": "7"}),
            (403, '{"reason": "userRateLimitExceeded"}'),
            (502, "bad gateway"),
        ]

        assert len(list(connector().open_reader().read("calendar"))) > 1
        assert slept[0] == 7 and len(slept) == 3

    def test_gives_up_when_google_stays_busy(self, google: FakeGoogle) -> None:
        google.fail_next = [(503, "busy")] * 10
        with pytest.raises(ConnectorError, match=r"Google Calendar kept failing.*error 503"):
            list(connector().open_reader().read("calendar"))

    def test_lost_answers_never_duplicate_writes(self, google: FakeGoogle, tmp_path: Path) -> None:
        from usmp.connectors.memory import MemoryConnector

        google.lose_answers = [
            ("/calendars", 502),
            ("/events/import", 500),
            ("/events/import", 503),
        ]
        source = MemoryConnector({"domain": "calendar"}, items=sample())

        result = migrate(source, connector(), bundle_path=tmp_path / "b.usmp")

        assert result.report is not None and result.report.ok, result.report
        assert google.lose_answers == []
        [cal_id] = [c for c in google.calendars if c.endswith("group.calendar.google.com")]
        uids = sorted(
            e["iCalUID"] for e in google.events[cal_id].values() if "recurringEventId" not in e
        )
        assert uids == ["d@x", "s@x"]

    def test_failed_creates_are_tried_again(self, google: FakeGoogle, tmp_path: Path) -> None:
        from usmp.connectors.memory import MemoryConnector

        google.refuse = [("/calendars", 500), ("/events/import", 502)]
        source = MemoryConnector({"domain": "calendar"}, items=sample())

        result = migrate(source, connector(), bundle_path=tmp_path / "b.usmp")

        assert result.report is not None and result.report.ok, result.report
        assert google.refuse == []
        assert len([c for c in google.calendars if c.endswith("group.calendar.google.com")]) == 1

    def test_own_write_refused_as_duplicate_is_recognised(self) -> None:
        answers = [(500, "backend error"), (409, "duplicate")]
        looks = [None, {"id": "ev1"}]

        class Session:
            def request(self, *a: object, **k: object) -> object:
                status, text = answers.pop(0)
                return type("R", (), {"status_code": status, "text": text, "headers": {}})()

        found = _Api(Session(), read_only=False).call(
            "POST", "/calendars/c/events/import", body={}, recover=lambda: looks.pop(0)
        )

        assert found == {"id": "ev1"}
        assert answers == [] and looks == []

    def test_duplicate_without_earlier_try_is_a_conflict(self) -> None:
        class Session:
            def request(self, *a: object, **k: object) -> object:
                return type("R", (), {"status_code": 409, "text": "dup", "headers": {}})()

        with pytest.raises(ConnectorError, match="already exists"):
            _Api(Session(), read_only=False).call(
                "POST", "/x", body={}, recover=lambda: pytest.fail("not ours to look for")
            )

    def test_creates_are_not_repeated_blindly(self) -> None:
        class Dropped:
            def request(self, *a: object, **k: object) -> object:
                raise ConnectionError("reset by peer")

        with pytest.raises(ConnectorError, match="not retried: it might have been saved"):
            _Api(Dropped(), read_only=False).call("POST", "/calendars", body={})

    def test_network_error(self) -> None:
        class Down:
            def request(self, *a: object, **k: object) -> object:
                raise ConnectionError("offline")

        with pytest.raises(ConnectorError, match="cannot reach Google Calendar"):
            _Api(Down(), read_only=True).call("GET", "/x")


@pytest.mark.parametrize(
    ("trigger", "minutes"),
    [
        ("-PT10M", 10),
        ("-PT1H30M", 90),
        ("-P1D", 1440),
        ("-P1DT2H", 1560),
        ("PT0S", 0),
        ("PT5M", None),
        ("-P29D", None),
        ("-PT90S", 1),
    ],
)
def test_minutes_before(trigger: str, minutes: int | None) -> None:
    assert _minutes_before(trigger) == minutes


class TestSignIn:
    def test_not_signed_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gcal.credentials, "get_secret", lambda *a: None)

        with pytest.raises(
            ConnectorError, match=r"usmp auth google --client-secrets client_secret\.json --write"
        ):
            gcal.session_factory("me@example.com", write=True)()
        with pytest.raises(ConnectorError, match=r"not signed in to Google as me@example\.com"):
            gcal.session_factory("me@example.com", write=False)()

    def token(self, expiry: datetime) -> str:
        return json.dumps(
            {
                "token": "t",
                "refresh_token": "r",
                "client_id": "c",
                "client_secret": "s",
                "token_uri": "https://oauth2.googleapis.com/token",
                "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )

    def test_valid_token_and_read_falls_back_to_write_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tokens = {"google:me:write": self.token(datetime.now(UTC) + timedelta(hours=1))}
        monkeypatch.setattr(gcal.credentials, "get_secret", lambda c, a, *_: tokens.get(f"{c}:{a}"))

        session = gcal.session_factory("me", write=False)()

        assert session.credentials.token == "t"  # noqa: S105 - fake token

    def test_expired_token_refreshes_or_explains(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from google.oauth2.credentials import Credentials

        saved: dict[str, str] = {}
        monkeypatch.setattr(
            gcal.credentials,
            "get_secret",
            lambda *a: self.token(datetime.now(UTC) - timedelta(hours=1)),
        )
        monkeypatch.setattr(gcal.credentials, "set_secret", lambda c, a, s: saved.update({a: s}))

        def refresh(creds: Credentials, request: object) -> None:
            creds.token = "fresh"  # noqa: S105 - fake token
            creds.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

        monkeypatch.setattr(Credentials, "refresh", refresh)
        fresh = gcal.session_factory("me", write=True)().credentials.token
        assert fresh == "fresh"
        assert "me:write" in saved

        def broken(creds: Credentials, request: object) -> None:
            raise RuntimeError("invalid_grant")

        monkeypatch.setattr(Credentials, "refresh", broken)
        with pytest.raises(ConnectorError, match="cannot refresh the Google sign-in"):
            gcal.session_factory("me", write=True)()

    def test_cli_sign_in(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        secrets = tmp_path / "client.json"
        secrets.write_text("{}")
        stored: dict[str, str] = {}
        calls: list[bool] = []

        def fake_sign_in(path: str, write: bool, open_browser: bool) -> tuple[str, str]:
            calls.append(open_browser)
            return "me@gmail.com", "TOKEN"

        monkeypatch.setattr(gcal, "sign_in", fake_sign_in)
        monkeypatch.setattr(
            "usmp.sdk.credentials.set_secret", lambda c, a, s: stored.update({a: s})
        )

        result = CliRunner().invoke(
            app, ["auth", "google", "--client-secrets", str(secrets), "--write", "--no-browser"]
        )

        assert result.exit_code == 0, result.output
        assert stored == {"me@gmail.com:write": "TOKEN"}
        assert "google:account=me@gmail.com" in result.output
        assert "TOKEN" not in result.output
        assert calls == [False]
        assert "private window" in result.output

    def test_cli_sign_in_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        secrets = tmp_path / "client.json"
        secrets.write_text("{}")

        def fail(path: str, write: bool, open_browser: bool) -> tuple[str, str]:
            raise ConnectorError("browser closed")

        monkeypatch.setattr(gcal, "sign_in", fail)
        result = CliRunner().invoke(app, ["auth", "google", "--client-secrets", str(secrets)])

        assert result.exit_code == 2
        assert "browser closed" in result.output


def test_info_label_defaults_to_account() -> None:
    assert connector().info().label == "me@example.com"


@pytest.mark.parametrize(
    ("props", "kept"),
    [
        ({"private": {"usmp_id": "usmp:x"}}, None),
        ({"private": {"usmp_id": "usmp:x", "mine": "1"}}, {"private": {"mine": "1"}}),
        ({"private": {"usmp_id": "usmp:x"}, "shared": {"a": "b"}}, {"shared": {"a": "b"}}),
        ({"shared": {"a": "b"}}, {"shared": {"a": "b"}}),
    ],
)
def test_usmp_marker_is_never_part_of_provider_data(
    props: dict[str, Any], kept: dict[str, Any] | None
) -> None:
    google: dict[str, Any] = {"extendedProperties": props}
    gcal._drop_marker(google)
    assert google.get("extendedProperties") == kept


@pytest.mark.parametrize(
    ("location", "written", "expected"),
    [
        ("Online, Meeting room", "Online", "Online"),
        ("Meeting room", "", ""),
        ("Café Central, Vienna, Meeting room", "Café Central, Vienna", "Café Central, Vienna"),
        ("Somewhere new", "Online", None),  # edited in Google since: keep what Google has
        ("Online", "Online", "Online"),
    ],
)
def test_location_written_before_google_added_rooms(
    location: str, written: str, expected: str | None
) -> None:
    ev = {
        "location": location,
        "attendees": [{"email": "r@rooms.example.com", "resource": True,
                       "displayName": "Meeting room"}],
        "extendedProperties": {"private": {"usmp_location": written}},
    }  # fmt: skip
    assert gcal._written_location(ev) == expected
    assert gcal._written_location({"location": location}) is None
