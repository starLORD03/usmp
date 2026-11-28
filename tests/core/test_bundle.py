import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from tests.conftest import make_event
from usmp.core.bundle import (
    BundleError,
    BundleLockedError,
    BundleReader,
    BundleWriter,
    Limits,
    is_encrypted,
)
from usmp.core.models import CapabilityManifest, ConnectorInfo, LossReason, LossRecord


@pytest.fixture
def bundle(tmp_path: Path, source_info: ConnectorInfo, capabilities: CapabilityManifest) -> Path:
    path = tmp_path / "out.usmp"
    items = [make_event(str(n)) for n in range(5)]
    with BundleWriter(path, source=source_info, capabilities=capabilities) as w:
        for item in reversed(items):
            w.add_item(item)
        w.add_loss(
            LossRecord(
                usmp_id=items[0].usmp_id,
                field="x-color",
                reason=LossReason.UNSUPPORTED_BY_CANONICAL,
                stage="export",
            )
        )
        w.add_raw("event-0.ics", b"BEGIN:VEVENT\r\nEND:VEVENT\r\n")
    return path


def rewrite(src: Path, dst: Path, change: dict[str, bytes | None]) -> Path:
    """Copy a bundle, replacing (bytes), removing (None) or adding entries."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            if info.filename in change:
                continue
            zout.writestr(info, zin.read(info))
        for name, data in change.items():
            if data is not None:
                zout.writestr(name, data)
    return dst


class TestRoundTrip:
    def test_contents_survive(self, bundle: Path, source_info: ConnectorInfo) -> None:
        with BundleReader(bundle) as r:
            items = list(r.iter_items())
            assert len(items) == 5
            assert [i.usmp_id for i in items] == sorted(i.usmp_id for i in items)
            assert r.manifest.source == source_info
            assert [(c.domain, c.type, c.count) for c in r.manifest.counts] == [
                ("demo", "event", 5)
            ]
            assert len(r.losses()) == 1
            assert r.raw_names == ["event-0.ics"]
            assert r.raw("event-0.ics").startswith(b"BEGIN:VEVENT")
            assert r.validate() == []

    def test_items_file_is_deterministic(
        self, tmp_path: Path, source_info: ConnectorInfo, capabilities: CapabilityManifest
    ) -> None:
        outputs = []
        for name, order in (("a.usmp", range(3)), ("b.usmp", reversed(range(3)))):
            with BundleWriter(tmp_path / name, source=source_info, capabilities=capabilities) as w:
                for n in order:
                    w.add_item(make_event(str(n)))
            with BundleReader(tmp_path / name) as r:
                outputs.append(r.items_bytes())
        assert outputs[0] == outputs[1]

    def test_missing_raw_entry(self, bundle: Path) -> None:
        with BundleReader(bundle) as r, pytest.raises(BundleError, match="no raw entry"):
            r.raw("nope")


class TestWriter:
    def test_duplicate_item_rejected(
        self, tmp_path: Path, source_info: ConnectorInfo, capabilities: CapabilityManifest
    ) -> None:
        w = BundleWriter(tmp_path / "x.usmp", source=source_info, capabilities=capabilities)
        w.add_item(make_event("1"))
        with pytest.raises(BundleError, match="duplicate item"):
            w.add_item(make_event("1"))

    @pytest.mark.parametrize("name", ["../evil", ".hidden", "a/b", "", "x" * 201])
    def test_bad_raw_names_rejected(
        self,
        tmp_path: Path,
        source_info: ConnectorInfo,
        capabilities: CapabilityManifest,
        name: str,
    ) -> None:
        w = BundleWriter(tmp_path / "x.usmp", source=source_info, capabilities=capabilities)
        with pytest.raises(BundleError, match="invalid raw entry name"):
            w.add_raw(name, b"")

    def test_duplicate_raw_rejected(
        self, tmp_path: Path, source_info: ConnectorInfo, capabilities: CapabilityManifest
    ) -> None:
        w = BundleWriter(tmp_path / "x.usmp", source=source_info, capabilities=capabilities)
        w.add_raw("a", b"")
        with pytest.raises(BundleError, match="duplicate raw"):
            w.add_raw("a", b"")

    def test_nothing_written_when_block_fails(
        self, tmp_path: Path, source_info: ConnectorInfo, capabilities: CapabilityManifest
    ) -> None:
        path = tmp_path / "x.usmp"

        def write_then_fail() -> None:
            with BundleWriter(path, source=source_info, capabilities=capabilities) as w:
                w.add_item(make_event("1"))
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            write_then_fail()

        assert not path.exists()
        assert list(tmp_path.iterdir()) == []


class TestTampering:
    def test_edited_items_detected(self, bundle: Path, tmp_path: Path) -> None:
        with zipfile.ZipFile(bundle) as z:
            lines = z.read("items.jsonl").decode().splitlines()
        first = json.loads(lines[0])
        first["payload"]["title"] = "tampered"
        lines[0] = json.dumps(first)
        bad = rewrite(bundle, tmp_path / "bad.usmp", {"items.jsonl": "\n".join(lines).encode()})

        with BundleReader(bad) as r:
            problems = r.validate()
            with pytest.raises(BundleError, match="line 1 is invalid"):
                list(r.iter_items())
        assert any("items_sha256" in p for p in problems)
        assert any("content_hash does not match" in p for p in problems)

    def test_removed_item_detected(self, bundle: Path, tmp_path: Path) -> None:
        with zipfile.ZipFile(bundle) as z:
            lines = z.read("items.jsonl").splitlines(keepends=True)
        bad = rewrite(bundle, tmp_path / "bad.usmp", {"items.jsonl": b"".join(lines[1:])})

        with BundleReader(bad) as r:
            problems = r.validate()
        assert any("counts" in p for p in problems)

    def test_loss_and_raw_counts_checked(self, bundle: Path, tmp_path: Path) -> None:
        bad = rewrite(
            bundle, tmp_path / "bad.usmp", {"losses.json": b"[]", "raw/event-0.ics": None}
        )
        with BundleReader(bad) as r:
            problems = r.validate()
        assert any("losses.json has 0 records" in p for p in problems)
        assert any("raw entries" in p for p in problems)

    def test_invalid_losses_reported(self, bundle: Path, tmp_path: Path) -> None:
        bad = rewrite(bundle, tmp_path / "bad.usmp", {"losses.json": b'[{"nope": 1}]'})
        with BundleReader(bad) as r:
            assert any("losses.json is invalid" in p for p in r.validate())

    def test_unsorted_and_duplicate_items_detected(self, bundle: Path, tmp_path: Path) -> None:
        with zipfile.ZipFile(bundle) as z:
            lines = z.read("items.jsonl").splitlines(keepends=True)
        bad = rewrite(
            bundle, tmp_path / "bad.usmp", {"items.jsonl": b"".join([lines[1], lines[0], lines[0]])}
        )
        with BundleReader(bad) as r:
            problems = r.validate()
        assert any("duplicate" in p for p in problems)
        assert any("not sorted" in p for p in problems)

    def test_loss_for_unknown_item_detected(self, bundle: Path, tmp_path: Path) -> None:
        loss = LossRecord(
            usmp_id=make_event("other").usmp_id,
            field="f",
            reason=LossReason.VALUE_CONVERTED,
            stage="export",
        )
        bad = rewrite(
            bundle, tmp_path / "bad.usmp", {"losses.json": f"[{loss.model_dump_json()}]".encode()}
        )
        with BundleReader(bad) as r:
            assert any("not in the bundle" in p for p in r.validate())


class TestUnsafeOrInvalidArchives:
    def test_not_a_zip(self, tmp_path: Path) -> None:
        path = tmp_path / "x.usmp"
        path.write_text("hello")
        with pytest.raises(BundleError, match="not a readable bundle"):
            BundleReader(path)

    @pytest.mark.parametrize("name", ["../escape.txt", "/etc/passwd", "raw/../../x", "extra.txt"])
    def test_unexpected_or_traversal_entries(self, bundle: Path, tmp_path: Path, name: str) -> None:
        bad = rewrite(bundle, tmp_path / "bad.usmp", {name: b"x"})
        with pytest.raises(BundleError, match=r"unexpected entry|invalid raw entry name"):
            BundleReader(bad)

    @pytest.mark.parametrize("name", ["manifest.json", "items.jsonl", "losses.json"])
    def test_missing_required_entry(self, bundle: Path, tmp_path: Path, name: str) -> None:
        bad = rewrite(bundle, tmp_path / "bad.usmp", {name: None})
        with pytest.raises(BundleError, match="missing required entry"):
            BundleReader(bad)

    def test_symlink_entry_rejected(self, bundle: Path, tmp_path: Path) -> None:
        bad = rewrite(bundle, tmp_path / "bad.usmp", {})
        with zipfile.ZipFile(bad, "a") as z:
            info = zipfile.ZipInfo("raw/link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "/etc/passwd")
        with pytest.raises(BundleError, match="symbolic link"):
            BundleReader(bad)

    def test_encrypted_entry_rejected(self, bundle: Path, tmp_path: Path) -> None:
        # zipfile cannot write encrypted entries, so set the flag in the central directory.
        data = bytearray(bundle.read_bytes())
        header = data.index(b"PK\x01\x02")  # first central directory record
        data[header + 8] |= 0x1  # general purpose flag bit 0: encrypted
        bad = tmp_path / "bad.usmp"
        bad.write_bytes(bytes(data))
        with pytest.raises(BundleError, match="encrypted"):
            BundleReader(bad)

    def test_zip_bomb_ratio_rejected(self, bundle: Path, tmp_path: Path) -> None:
        bad = rewrite(bundle, tmp_path / "bad.usmp", {})
        with zipfile.ZipFile(bad, "a", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("raw/bomb", b"\0" * (4 * 1024 * 1024))
        with pytest.raises(BundleError, match="compression ratio"):
            BundleReader(bad)

    def test_size_limits(self, bundle: Path) -> None:
        with pytest.raises(BundleError, match="too large"):
            BundleReader(bundle, limits=Limits(max_entry_bytes=10))
        with pytest.raises(BundleError, match="too large when uncompressed"):
            BundleReader(bundle, limits=Limits(max_total_bytes=100))
        with pytest.raises(BundleError, match="too many entries"):
            BundleReader(bundle, limits=Limits(max_entries=2))

    def test_duplicate_entry_rejected(self, bundle: Path, tmp_path: Path) -> None:
        bad = rewrite(bundle, tmp_path / "bad.usmp", {})
        with (
            pytest.warns(UserWarning, match="Duplicate name"),
            zipfile.ZipFile(bad, "a") as z,
        ):
            z.writestr("losses.json", b"[]")
        with pytest.raises(BundleError, match="duplicate entry"):
            BundleReader(bad)

    @pytest.mark.parametrize(
        ("manifest", "message"),
        [
            (b"not json", "not valid JSON"),
            (b'{"format": "zip"}', "does not describe a USMP bundle"),
            (b'{"format": "usmp-bundle", "format_version": 99}', "unsupported bundle format"),
            (b'{"format": "usmp-bundle", "format_version": 1}', "manifest.json is invalid"),
        ],
    )
    def test_bad_manifests(
        self, bundle: Path, tmp_path: Path, manifest: bytes, message: str
    ) -> None:
        bad = rewrite(bundle, tmp_path / "bad.usmp", {"manifest.json": manifest})
        with pytest.raises(BundleError, match=message):
            BundleReader(bad)

    def test_lying_size_header_detected(self, bundle: Path, tmp_path: Path) -> None:
        bad = tmp_path / "bad.usmp"
        bad.write_bytes(bundle.read_bytes())
        with BundleReader(bad) as r:
            r._infos["losses.json"].file_size -= 1  # simulate a header that under-reports
            with pytest.raises(BundleError, match="corrupt"):
                r.losses()


PASSPHRASE = "correct horse battery"  # noqa: S105 - test value
WRONG = "not the right one"
SHORT = "short"


class TestEncryption:
    @pytest.fixture
    def locked(
        self, tmp_path: Path, source_info: ConnectorInfo, capabilities: CapabilityManifest
    ) -> Path:
        path = tmp_path / "locked.usmp"
        with BundleWriter(
            path, source=source_info, capabilities=capabilities, passphrase=PASSPHRASE
        ) as w:
            w.add_item(make_event("1"))
        return path

    def test_round_trip_with_the_passphrase(self, locked: Path) -> None:
        assert locked.read_bytes().startswith(b"age-encryption.org/v1\n")
        assert b"demo" not in locked.read_bytes(), "nothing readable without the passphrase"
        assert is_encrypted(locked)
        with BundleReader(locked, passphrase=PASSPHRASE) as r:
            assert r.encrypted
            assert len(list(r.iter_items())) == 1
            assert r.validate() == []
        if os.name != "nt":  # POSIX file modes
            assert stat.S_IMODE(locked.stat().st_mode) & 0o077 == 0, "owner only"

    def test_no_passphrase_is_refused_not_guessed(self, locked: Path) -> None:
        with pytest.raises(BundleLockedError, match="encrypted: give its passphrase"):
            BundleReader(locked)

    def test_wrong_passphrase(self, locked: Path) -> None:
        with pytest.raises(BundleError, match="wrong passphrase, or the backup is damaged"):
            BundleReader(locked, passphrase=WRONG)

    def test_damaged_encrypted_bundle(self, locked: Path) -> None:
        data = bytearray(locked.read_bytes())
        data[-5] ^= 0xFF
        locked.write_bytes(bytes(data))
        with pytest.raises(BundleError, match="wrong passphrase, or the backup is damaged"):
            BundleReader(locked, passphrase=PASSPHRASE)

    def test_plain_bundle_ignores_a_passphrase(self, bundle: Path) -> None:
        assert not is_encrypted(bundle)
        with BundleReader(bundle, passphrase=PASSPHRASE) as r:
            assert not r.encrypted

    def test_short_passphrase_is_refused(
        self, tmp_path: Path, source_info: ConnectorInfo, capabilities: CapabilityManifest
    ) -> None:
        path = tmp_path / "x.usmp"
        with (
            pytest.raises(ValueError, match="at least 8 characters"),
            BundleWriter(path, source=source_info, capabilities=capabilities, passphrase=SHORT),
        ):
            pass
        assert not path.exists()

    def test_too_large_to_decrypt(self, locked: Path) -> None:
        with pytest.raises(BundleError, match="too large"):
            BundleReader(locked, passphrase=PASSPHRASE, limits=Limits(max_total_bytes=10))
