from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from usmp.core.models import (
    CapabilityManifest,
    ConnectorInfo,
    DomainCapability,
    Item,
    ItemResult,
    ItemStatus,
    LossReason,
    LossRecord,
    ReportSummary,
    TypeCapability,
    VerificationReport,
)


def make_item(**payload: object) -> Item:
    return Item.create(
        connector="memory",
        native_id="evt-1",
        domain="calendar",
        type="event",
        schema_version=1,
        payload=payload or {"title": "Standup", "start": "2026-01-05T09:00:00Z"},
    )


class TestItem:
    def test_create_derives_id_and_hash(self) -> None:
        item = make_item()

        assert item.usmp_id.startswith("usmp:")
        assert item.content_hash.startswith("sha256:")
        assert item.source.connector == "memory"

    def test_json_round_trip(self) -> None:
        item = make_item()

        assert Item.model_validate_json(item.model_dump_json()) == item

    def test_tampered_payload_is_rejected(self) -> None:
        data = make_item().model_dump()
        data["payload"]["title"] = "Changed"

        with pytest.raises(ValidationError, match="content_hash does not match"):
            Item.model_validate(data)

    def test_extensions_and_source_do_not_affect_hash(self) -> None:
        a = make_item()
        b = Item.create(
            connector="other",
            native_id="x",
            domain="calendar",
            type="event",
            schema_version=1,
            payload=a.payload,
            extensions={"other": {"color": "blue"}},
        )

        assert a.content_hash == b.content_hash

    def test_projected_hash_ignores_other_fields(self) -> None:
        a = make_item(title="Standup", color="red")
        b = make_item(title="Standup", color="blue")

        assert a.content_hash != b.content_hash
        assert a.projected_hash({"title"}) == b.projected_hash({"title"})

    @pytest.mark.parametrize("field", ["domain", "type"])
    def test_names_must_be_lower_snake_case(self, field: str) -> None:
        data = make_item().model_dump()
        data[field] = "Bad-Name"

        with pytest.raises(ValidationError):
            Item.model_validate(data)

    def test_unknown_fields_rejected(self) -> None:
        data = make_item().model_dump()
        data["surprise"] = 1

        with pytest.raises(ValidationError, match="Extra inputs"):
            Item.model_validate(data)

    def test_items_are_immutable(self) -> None:
        item = make_item()

        with pytest.raises(ValidationError):
            item.domain = "contacts"  # type: ignore[misc]


def manifest() -> CapabilityManifest:
    return CapabilityManifest(
        connector="memory",
        display_name="Memory",
        version="1",
        domains=[
            DomainCapability(
                domain="calendar",
                schema_version=1,
                types=[
                    TypeCapability(
                        type="event", read_fields=["title", "start", "title"], write_fields=[]
                    )
                ],
            )
        ],
    )


class TestCapabilityManifest:
    def test_field_lists_are_sorted_and_deduplicated(self) -> None:
        cap = manifest().type_capability("calendar", "event")

        assert cap is not None
        assert cap.read_fields == ["start", "title"]

    def test_queries(self) -> None:
        m = manifest()

        assert m.can_read("calendar")
        assert not m.can_write("calendar")
        assert not m.can_read("contacts")
        assert m.type_capability("calendar", "todo") is None
        assert m.type_capability("contacts", "event") is None

    def test_duplicate_types_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate types"):
            DomainCapability(
                domain="calendar",
                schema_version=1,
                types=[TypeCapability(type="event"), TypeCapability(type="event")],
            )

    def test_duplicate_domains_rejected(self) -> None:
        d = DomainCapability(domain="calendar", schema_version=1, types=[])
        with pytest.raises(ValidationError, match="duplicate domains"):
            CapabilityManifest(connector="m", display_name="M", version="1", domains=[d, d])


def report(results: list[ItemResult], **overrides: object) -> VerificationReport:
    now = datetime.now(UTC)
    fields: dict[str, object] = {
        "run_id": "run-1",
        "started_at": now,
        "finished_at": now + timedelta(seconds=1),
        "domain": "calendar",
        "dry_run": False,
        "source": ConnectorInfo(connector="memory", display_name="Memory"),
        "target": ConnectorInfo(connector="memory", display_name="Memory"),
        "summary": ReportSummary.from_results(results),
        "items": results,
        "losses": [],
    }
    fields.update(overrides)
    return VerificationReport.model_validate(fields)


class TestVerificationReport:
    item_id = make_item().usmp_id

    def result(self, status: ItemStatus) -> ItemResult:
        return ItemResult(usmp_id=self.item_id, type="event", status=status)

    def test_ok_when_only_verified(self) -> None:
        r = report(
            [self.result(ItemStatus.VERIFIED), self.result(ItemStatus.VERIFIED_WITH_DECLARED_LOSS)]
        )

        assert r.ok
        assert r.summary.total == 2
        assert '"ok":true' in r.model_dump_json()

    @pytest.mark.parametrize("bad", [ItemStatus.MISMATCH, ItemStatus.MISSING])
    def test_not_ok_on_mismatch_or_missing(self, bad: ItemStatus) -> None:
        assert not report([self.result(ItemStatus.VERIFIED), self.result(bad)]).ok

    def test_summary_must_match_items(self) -> None:
        wrong = ReportSummary(
            total=1, verified=0, verified_with_declared_loss=0, mismatch=0, missing=1
        )
        with pytest.raises(ValidationError, match="summary does not match"):
            report([self.result(ItemStatus.VERIFIED)], summary=wrong)

    def test_finish_before_start_rejected(self) -> None:
        now = datetime.now(UTC)
        with pytest.raises(ValidationError, match="before started_at"):
            report([], started_at=now, finished_at=now - timedelta(seconds=1))


def test_loss_record_defaults() -> None:
    loss = LossRecord(
        usmp_id=make_item().usmp_id,
        field="color",
        reason=LossReason.UNSUPPORTED_BY_TARGET,
        stage="plan",
    )

    assert loss.severity == "warning"
    assert loss.model_dump(mode="json")["reason"] == "unsupported_by_target"


class TestWildcards:
    def test_wildcard_type_and_fields(self) -> None:
        m = CapabilityManifest(
            connector="jsondir",
            display_name="JSON directory",
            version="1",
            domains=[
                DomainCapability(
                    domain="calendar",
                    schema_version=1,
                    types=[
                        TypeCapability(type="*", read_fields=["*"], write_fields=["*"]),
                        TypeCapability(type="event", read_fields=["title"], write_fields=[]),
                    ],
                )
            ],
        )

        exact = m.type_capability("calendar", "event")
        fallback = m.type_capability("calendar", "todo")
        assert exact is not None
        assert fallback is not None
        assert exact.readable({"title", "start"}) == {"title"}
        assert exact.writable({"title"}) == set()
        assert fallback.type == "*"
        assert fallback.readable({"a", "b"}) == {"a", "b"}
        assert fallback.writable({"a"}) == {"a"}

    def test_wildcard_must_be_alone(self) -> None:
        with pytest.raises(ValidationError, match="must be the only entry"):
            TypeCapability(type="event", read_fields=["*", "title"])


def test_report_json_round_trips_and_recomputes_ok() -> None:
    item_id = make_item().usmp_id
    r = report([ItemResult(usmp_id=item_id, type="event", status=ItemStatus.MISSING)])
    data = r.model_dump(mode="json")
    data["ok"] = True  # edited claim

    parsed = VerificationReport.model_validate(data)

    assert parsed == r
    assert parsed.ok is False
