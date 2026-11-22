from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from usmp.cli.app import app
from usmp.connectors.ics import IcsConnector
from usmp.core.bundle import BundleReader
from usmp.core.engine import migrate
from usmp.core.models import Item, ItemStatus, LossReason
from usmp.domains.calendar import calendar_item, canonicalize, event_item
from usmp.sdk.connector import ConnectorError, ExportRecord
from usmp.testing import ConnectorConformance

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ics"


def read(path: Path) -> list[ExportRecord]:
    return list(IcsConnector({"path": str(path)}).open_reader().read("calendar"))


def events(records: list[ExportRecord]) -> dict[str, dict[str, Any]]:
    return {r.item.source.native_id: r.item.payload for r in records if r.item.type == "event"}


def losses(records: list[ExportRecord]) -> set[tuple[str, str]]:
    return {(loss.field, loss.reason.value) for r in records for loss in r.losses}


class TestReadingRealWorldExports:
    def test_outlook_windows_zone_recurrence_and_override(self) -> None:
        records = read(FIXTURES / "outlook.ics")
        ev = events(records)
        standup = ev["outlook.ics#standup-1@example.com"]
        moved = ev["outlook.ics#standup-1@example.com#20260107T090000"]

        assert records[0].item.type == "calendar"
        assert records[0].item.payload == {"name": "Work"}
        assert standup["time_zone"] == "Europe/Berlin"
        assert standup["start"] == "2026-01-05T09:00:00"
        assert standup["excluded_dates"] == ["2026-01-12T09:00:00"]
        assert standup["attendees"] == [
            {"email": "amy@example.com", "name": "Amy", "role": "optional", "status": "accepted"}
        ]
        assert moved["recurrence_id"] == "2026-01-07T09:00:00"
        assert ev["outlook.ics#holiday@example.com"]["start"] == "2026-01-01"
        assert ("X-MICROSOFT-CDO-BUSYSTATUS", "unsupported_by_canonical") in losses(records)
        assert ("VTODO", "unsupported_by_canonical") in losses(records)
        assert all(r.raw is not None for r in records if r.item.type == "event")

    def test_google_utc_duration_and_bookkeeping(self) -> None:
        records = read(FIXTURES / "google.ics")
        ev = events(records)
        dentist = ev["google.ics#abc123@google.com"]
        gym = ev["google.ics#gym@google.com"]

        assert records[0].item.payload == {
            "name": "Personal",
            "description": "My stuff",
            "time_zone": "America/New_York",
        }
        assert dentist["time_zone"] == "UTC"
        assert dentist["alarms"] == [{"trigger": "-PT30M"}]
        assert "description" not in dentist, "empty DESCRIPTION is dropped"
        assert gym["end"] == "2026-04-01T08:45:00"
        assert gym["recurrence_rules"] == ["FREQ=WEEKLY;BYDAY=WE;UNTIL=20260601T120000Z"]
        assert gym["attachments"] == [
            {
                "uri": "https://drive.example/plan",
                "media_type": "application/pdf",
                "title": "plan.pdf",
            }
        ]
        found = losses(records)
        assert ("X-GOOGLE-CONFERENCE", "unsupported_by_canonical") in found
        assert not any(f in {"DTSTAMP", "CREATED", "LAST-MODIFIED", "SEQUENCE"} for f, _ in found)

    def test_directory_of_files_is_one_calendar_per_file(self) -> None:
        records = read(FIXTURES)

        assert [r.item.payload["name"] for r in records if r.item.type == "calendar"] == [
            "Personal",
            "Work",
        ]


def ics(tmp_path: Path, body: str, name: str = "x.ics") -> Path:
    path = tmp_path / name
    # Bytes, not text: on Windows write_text would turn \r\n into \r\r\n.
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:t\r\n"
        + body.strip().replace("\n", "\r\n")
        + "\r\nEND:VCALENDAR\r\n"
    )
    path.write_bytes(text.encode())
    return path


def vevent(*lines: str, uid: str = "u1") -> str:
    return "\n".join(["BEGIN:VEVENT", f"UID:{uid}", *lines, "END:VEVENT"])


class TestEdgeCases:
    def test_unknown_zone_is_converted_to_utc_with_loss(self, tmp_path: Path) -> None:
        body = vevent("DTSTART;TZID=Custom/Nowhere:20260105T090000")
        body = body.replace(
            "BEGIN:VEVENT",
            "BEGIN:VTIMEZONE\nTZID:Custom/Nowhere\nBEGIN:STANDARD\n"
            "DTSTART:19700101T000000\nTZOFFSETFROM:+0300\nTZOFFSETTO:+0300\n"
            "END:STANDARD\nEND:VTIMEZONE\nBEGIN:VEVENT",
            1,
        )
        records = read(ics(tmp_path, body))
        [ev] = events(records).values()

        assert ev["time_zone"] == "UTC"
        assert ev["start"] == "2026-01-05T06:00:00"
        assert ("time_zone", "value_converted") in losses(records)

    def test_utc_exdate_is_expressed_in_event_zone(self, tmp_path: Path) -> None:
        records = read(
            ics(
                tmp_path,
                vevent(
                    "DTSTART;TZID=Europe/Paris:20260105T090000",
                    "RRULE:FREQ=DAILY",
                    "EXDATE:20260106T080000Z",
                ),
            )
        )

        assert next(iter(events(records).values()))["excluded_dates"] == ["2026-01-06T09:00:00"]

    def test_floating_time_and_different_end_zone(self, tmp_path: Path) -> None:
        floating = read(ics(tmp_path, vevent("DTSTART:20260105T090000"), "a.ics"))
        split = read(
            ics(
                tmp_path,
                vevent(
                    "DTSTART;TZID=America/New_York:20260105T200000",
                    "DTEND;TZID=Europe/London:20260106T080000",
                ),
                "b.ics",
            )
        )

        assert "time_zone" not in next(iter(events(floating).values()))
        ev = next(iter(events(split).values()))
        assert (ev["time_zone"], ev["end_time_zone"]) == ("America/New_York", "Europe/London")

    def test_zero_length_events(self, tmp_path: Path) -> None:
        timed = read(
            ics(tmp_path, vevent("DTSTART:20260105T090000Z", "DTEND:20260105T090000Z"), "a.ics")
        )
        allday = read(
            ics(
                tmp_path,
                vevent("DTSTART;VALUE=DATE:20260105", "DTEND;VALUE=DATE:20260105"),
                "b.ics",
            )
        )

        assert "end" not in next(iter(events(timed).values()))
        assert ("end", "value_converted") in losses(allday)

    def test_unsupported_values_become_losses(self, tmp_path: Path) -> None:
        records = read(
            ics(
                tmp_path,
                vevent(
                    "DTSTART:20260105T090000Z",
                    "STATUS:IN-PROCESS",
                    "CLASS:SECRET",
                    "COLOR:turquoise",
                    "RDATE;VALUE=PERIOD:20260110T090000Z/PT1H",
                    "ORGANIZER:CN=nobody",
                    "ATTENDEE;CUTYPE=ROOM;X-EXTRA=1:mailto:room@x.com",
                    "ATTENDEE:mailto:room@x.com",
                    "ATTENDEE;ROLE=WEIRD;PARTSTAT=COMPLETED:mailto:a@x.com",
                    "ATTENDEE:noemail",
                    "ATTACH;ENCODING=BASE64;VALUE=BINARY:aGVsbG8=",
                    "BEGIN:VALARM",
                    "TRIGGER;VALUE=DATE-TIME:20260105T080000Z",
                    "ACTION:DISPLAY",
                    "END:VALARM",
                    "BEGIN:VALARM",
                    "TRIGGER;RELATED=END:-PT5M",
                    "ACTION:DISPLAY",
                    "END:VALARM",
                    "BEGIN:VALARM",
                    "TRIGGER:-PT5M",
                    "ACTION:PROCEDURE",
                    "END:VALARM",
                ),
            )
        )
        found = losses(records)

        for name in [
            "status",
            "privacy",
            "color",
            "recurrence_dates",
            "organizer",
            "attendees",
            "attachments",
            "alarms",
        ]:
            assert any(f == name for f, _ in found), name
        [ev] = events(records).values()
        assert [a["email"] for a in ev["attendees"]] == ["a@x.com", "room@x.com"]

    def test_missing_uid_is_generated(self, tmp_path: Path) -> None:
        path = ics(tmp_path, "BEGIN:VEVENT\nDTSTART:20260105T090000Z\nEND:VEVENT")
        [ev] = events(read(path)).values()

        assert ev["uid"].startswith("usmp-")

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ("BEGIN:VEVENT\nUID:x\nSUMMARY:no start\nEND:VEVENT", "has no DTSTART"),
            (vevent("DTSTART:20260105T090000Z", "DTEND:20260105T080000Z"), "cannot be represented"),
            (
                vevent("DTSTART:20260105T090000Z") + "\n" + vevent("DTSTART:20260106T090000Z"),
                "appears more than once",
            ),
        ],
    )
    def test_unrepresentable_input_is_a_clear_error(
        self, tmp_path: Path, body: str, message: str
    ) -> None:
        with pytest.raises(ConnectorError, match=message):
            read(ics(tmp_path, body))

    def test_not_an_ics_file(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.ics"
        bad.write_text("hello")
        with pytest.raises(ConnectorError, match="cannot read"):
            read(bad)

    def test_missing_path(self, tmp_path: Path) -> None:
        from usmp.connectors.ics import _files

        with pytest.raises(ConnectorError, match="does not exist"):
            IcsConnector({"path": str(tmp_path / "nope.ics")}).open_reader()
        with pytest.raises(ConnectorError, match="does not exist"):
            _files(tmp_path / "gone")

    def test_other_domains_yield_nothing(self) -> None:
        assert list(IcsConnector({"path": str(FIXTURES)}).open_reader().read("contacts")) == []


# --- Writing -----------------------------------------------------------------------------------


def sample() -> list[Item]:
    cal = calendar_item(
        "seed",
        "c1",
        {
            "name": "Team Plans",
            "color": "#33b679",
            "description": "Shared",
            "time_zone": "Europe/Paris",
        },
    )
    base = {
        "uid": "s1@x",
        "start": "2026-05-04T09:00:00",
        "end": "2026-05-04T10:00:00",
        "time_zone": "Europe/Paris",
        "title": "Sync",
        "recurrence_rules": ["FREQ=WEEKLY"],
        "excluded_dates": ["2026-05-11T09:00:00"],
    }
    return [
        cal,
        event_item("seed", "e1", base, parent=cal.usmp_id),
        event_item(
            "seed",
            "e2",
            {
                "uid": "s1@x",
                "start": "2026-05-18T11:00:00",
                "time_zone": "Europe/Paris",
                "recurrence_id": "2026-05-18T09:00:00",
                "title": "Sync (moved)",
            },
            parent=cal.usmp_id,
        ),
        event_item("seed", "e3", {"uid": "orphan@x", "start": "2026-06-01"}),
    ]


class TestIcsConformance(ConnectorConformance):
    domain = "calendar"

    @pytest.fixture(autouse=True)
    def _dir(self, tmp_path: Path) -> None:
        self.path = tmp_path / "out"
        self.path.mkdir()

    def make_connector(self) -> IcsConnector:
        return IcsConnector({"path": str(self.path)})

    def sample_items(self) -> list[Item]:
        return sample()

    def provider_state(self) -> Any:
        return {p.name: p.read_bytes() for p in self.path.glob("*.ics")}


class TestWriting:
    def test_directory_mode_files_and_default_calendar(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        results = list(IcsConnector({"path": str(out)}).open_writer().write(sample()))

        assert sorted(p.name for p in out.iterdir()) == ["imported.ics", "team-plans.ics"]
        assert results[2].native_id == "team-plans.ics#s1@x#20260518T090000"
        assert read(out / "team-plans.ics")[0].item.payload == sample()[0].payload

    def test_single_file_mode(self, tmp_path: Path) -> None:
        target = tmp_path / "one.ics"
        items = sample()[:3]
        list(IcsConnector({"path": str(target)}).open_writer().write(items))

        assert {r.item.content_hash for r in read(target)} == {i.content_hash for i in items}

    def test_single_file_with_several_calendars_fails(self, tmp_path: Path) -> None:
        cals = [calendar_item("s", str(n), {"name": f"C{n}"}) for n in range(2)]
        with pytest.raises(ConnectorError, match="several calendars"):
            list(IcsConnector({"path": str(tmp_path / "one.ics")}).open_writer().write(cals))

    def test_refuses_to_overwrite_and_dedupes_names(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "work.ics").write_text("keep me")
        cals = [
            calendar_item("s", "a", {"name": "Work"}),
            calendar_item("s", "b", {"name": "work"}),
        ]

        results = list(IcsConnector({"path": str(out)}).open_writer().write(cals))

        assert [r.native_id for r in results] == ["work-2.ics", "work-3.ics"]
        assert (out / "work.ics").read_text() == "keep me"
        single = IcsConnector({"path": str(tmp_path / "x.ics")})
        list(single.open_writer().write(cals[:1]))
        with pytest.raises(ConnectorError, match="refusing to overwrite"):
            list(single.open_writer().write(cals[:1]))

    def test_rejects_other_items_and_duplicate_events(self, tmp_path: Path) -> None:
        w = IcsConnector({"path": str(tmp_path / "o")}).open_writer()
        other = Item.create(
            connector="s", native_id="1", domain="notes", type="note", schema_version=1, payload={}
        )
        with pytest.raises(ConnectorError, match="cannot write notes/note"):
            list(w.write([other]))
        dup = event_item("s", "1", {"uid": "d", "start": "2026-01-01"})
        dup2 = event_item("s", "2", {"uid": "d", "start": "2026-01-02"})
        with pytest.raises(ConnectorError, match="appears more than once"):
            list(w.write([dup, dup2]))


# --- Round trip: any canonical event survives write and read -----------------------------------

zones = st.sampled_from(
    ["UTC", "Europe/Paris", "America/New_York", "Asia/Kolkata", "Australia/Sydney"]
)
text = (
    st.text(
        alphabet=st.characters(codec="utf-8", exclude_categories=("Cs", "Cc")),
        min_size=1,
        max_size=30,
    )
    .map(str.strip)
    .filter(bool)
)


@st.composite
def canonical_events(draw: st.DrawFn) -> dict[str, Any]:
    all_day = draw(st.booleans())
    day = draw(st.integers(1, 20))
    payload: dict[str, Any] = {"uid": f"{draw(st.integers(0, 10**6))}@hyp"}
    if all_day:
        payload["start"] = f"2026-07-{day:02d}"
        payload["end"] = f"2026-07-{day + draw(st.integers(1, 5)):02d}"

        def stamp(d: int) -> str:
            return f"2026-07-{d:02d}"

    else:
        hour = draw(st.integers(0, 20))
        payload["start"] = f"2026-07-{day:02d}T{hour:02d}:15:00"
        payload["end"] = f"2026-07-{day:02d}T{hour + 1:02d}:45:00"
        payload["time_zone"] = draw(st.one_of(st.none(), zones))

        def stamp(d: int) -> str:
            return f"2026-07-{d:02d}T{hour:02d}:15:00"

    payload["title"] = draw(st.one_of(st.none(), text))
    payload["description"] = draw(st.one_of(st.none(), text))
    payload["location"] = draw(st.one_of(st.none(), text))
    payload["status"] = draw(st.sampled_from([None, "confirmed", "tentative", "cancelled"]))
    payload["availability"] = draw(st.sampled_from([None, "busy", "free"]))
    payload["privacy"] = draw(st.sampled_from([None, "public", "private", "confidential"]))
    if draw(st.booleans()):
        payload["recurrence_rules"] = [
            draw(
                st.sampled_from(
                    ["FREQ=DAILY;COUNT=10", "FREQ=WEEKLY;BYDAY=MO,WE", "FREQ=MONTHLY;INTERVAL=2"]
                )
            )
        ]
        payload["excluded_dates"] = [
            stamp(day + n) for n in draw(st.sets(st.integers(1, 6), max_size=3))
        ]
    payload["attendees"] = [
        {
            "email": f"{n}@example.com",
            "name": draw(st.one_of(st.none(), text)),
            "role": draw(st.sampled_from(["chair", "required", "optional", "non_participant"])),
            "status": draw(st.sampled_from(["needs_action", "accepted", "declined", "tentative"])),
        }
        for n in draw(st.sets(st.sampled_from("abcdef"), max_size=3))
    ]
    payload["organizer"] = draw(st.one_of(st.none(), st.just({"email": "org@example.com"})))
    payload["alarms"] = [
        {"trigger": t, "action": a}
        for t, a in draw(
            st.sets(
                st.tuples(
                    st.sampled_from(["-PT5M", "-PT1H", "-P1D", "PT0S"]),
                    st.sampled_from(["display", "email", "audio"]),
                ),
                max_size=2,
            )
        )
    ]
    payload["categories"] = draw(
        st.lists(text.filter(lambda s: "," not in s and "\\" not in s), max_size=3)
    )
    payload["color"] = draw(st.one_of(st.none(), st.sampled_from(["#1a73e8", "#33b679"])))
    if draw(st.booleans()):
        payload["attachments"] = [
            {"uri": "https://files.example/a.pdf", "media_type": "application/pdf"}
        ]
    return canonicalize("event", payload)


@settings(
    max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(canonical_events())
def test_any_canonical_event_round_trips_through_ics(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    import tempfile

    item = event_item("seed", "1", payload)
    target = Path(tempfile.mkdtemp(dir=tmp_path)) / "cal.ics"

    writer = IcsConnector({"path": str(target)}).open_writer()
    [result] = list(writer.write([item]))
    [back] = list(writer.read_back("calendar", [result.native_id]))

    assert back.payload == item.payload
    assert back.content_hash == item.content_hash


# --- End to end ---------------------------------------------------------------------------------


def test_migrate_outlook_export_to_new_ics_directory(tmp_path: Path) -> None:
    result = migrate(
        IcsConnector({"path": str(FIXTURES / "outlook.ics")}),
        IcsConnector({"path": str(tmp_path / "new")}),
        bundle_path=tmp_path / "b.usmp",
    )

    assert result.report is not None and result.report.ok, result.report
    assert result.report.summary.total == 4
    with BundleReader(tmp_path / "b.usmp") as bundle:
        assert len(bundle.raw_names) == 3
        assert any(loss.reason is LossReason.UNSUPPORTED_BY_CANONICAL for loss in bundle.losses())


def test_cli_ics_to_jsondir(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "migrate",
            "--from",
            f"ics:path={FIXTURES}",
            "--to",
            f"jsondir:path={tmp_path / 'j'}",
            "--domain",
            "calendar",
            "--bundle",
            str(tmp_path / "b.usmp"),
            "--report-dir",
            str(tmp_path / "r"),
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Migration verified" in result.output


def _backslash_item(name: str) -> Item:
    return event_item(
        "seed",
        "1",
        {
            "uid": "b@x",
            "start": "2026-01-01",
            "organizer": {"email": "o@x.com", "name": name},
            "attendees": [{"email": "a@x.com", "name": name}],
            "attachments": [{"uri": "https://f/x", "title": name}],
        },
    )


@pytest.mark.parametrize("name", ["\\", "a\\", "Ünïcödé \\ b"])
def test_names_ending_in_backslash_round_trip(tmp_path: Path, name: str) -> None:
    # icalendar corrupts a parameter that ends in a backslash; found by the property test above.
    item = _backslash_item(name)
    writer = IcsConnector({"path": str(tmp_path / "c.ics")}).open_writer()
    [result] = list(writer.write([item]))
    [back] = list(writer.read_back("calendar", [result.native_id]))

    assert back.payload == item.payload
    assert result.losses == ()


def test_text_icalendar_cannot_store_is_declared_not_mismatched(tmp_path: Path) -> None:
    from usmp.connectors.memory import MemoryConnector

    item = _backslash_item("two \\\\ backslashes")  # icalendar collapses "\\\\" in parameters
    result = migrate(
        MemoryConnector({"domain": "calendar"}, items=[item]),
        IcsConnector({"path": str(tmp_path / "out")}),
        bundle_path=tmp_path / "b.usmp",
    )

    assert result.report is not None and result.report.ok, result.report
    [verdict] = result.report.items
    assert verdict.status is ItemStatus.VERIFIED_WITH_DECLARED_LOSS
    assert {"organizer", "attendees", "attachments"} <= set(verdict.declared_loss_fields)
