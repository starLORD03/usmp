# Writing a connector

A **connector** teaches USMP to talk to one provider: a calendar service, a cloud, a car. This
guide walks through building one and proving it follows the rules.

## 1. The contract in one minute

| You implement | It must |
|---|---|
| `capabilities()` | Declare, per domain and item type, which payload fields you can **read** and **write**. |
| `open_reader()` → `Reader.read(domain)` | Yield every item of the domain, parents before children. **Never change the provider.** |
| `open_writer()` → `Writer.write(items)` | Write items and yield the provider's id for each one as soon as it is stored (any order). |
| `Writer.already_written(items, known)` *(optional)* | Find items an earlier run already wrote, so re-runs and resumed runs write nothing twice. |
| `Writer.read_back(domain, ids)` | Return items as the provider now stores them, so USMP can verify them. |

Anything you can't map into the canonical model must be reported as a `LossRecord` with reason
`unsupported_by_canonical`. USMP never lets data disappear silently.

## 2. A minimal connector

```python
from collections.abc import Iterable, Iterator, Sequence

from usmp.core.models import CapabilityManifest, DomainCapability, Item, TypeCapability
from usmp.sdk import Connector, ExportRecord, Reader, WriteResult, Writer

FIELDS = ["title", "start", "end"]


class AcmeConnector(Connector):
    name = "acme"
    display_name = "Acme Calendar"
    config_keys = {"account": "your Acme account name"}

    def capabilities(self) -> CapabilityManifest:
        return CapabilityManifest(
            connector=self.name,
            display_name=self.display_name,
            version="0.1.0",
            domains=[
                DomainCapability(
                    domain="calendar",
                    schema_version=1,
                    types=[TypeCapability(type="event", read_fields=FIELDS, write_fields=FIELDS)],
                )
            ],
        )

    def open_reader(self) -> Reader:
        return AcmeReader(self.require("account"))

    def open_writer(self) -> Writer:
        return AcmeWriter(self.require("account"))


class AcmeReader(Reader):
    def __init__(self, account: str) -> None:
        self.api = acme_client(account)  # read-only credentials where the provider allows it

    def read(self, domain: str) -> Iterator[ExportRecord]:
        for event in self.api.list_events():
            yield ExportRecord(
                item=Item.create(
                    connector="acme",
                    native_id=event.id,
                    domain="calendar",
                    type="event",
                    schema_version=1,
                    payload={"title": event.name, "start": event.begins, "end": event.ends},
                ),
                raw=event.json_bytes,  # optional: the original, kept in the backup bundle
                raw_name=f"{event.id}.json",
            )
```

!!! warning "Keep readers read-only"
    A `Reader` must not have methods such as `delete_…`, `update_…` or `write`. The conformance
    kit fails if it finds any. Where the provider supports it, use read-only OAuth scopes for the
    source.

## 3. Register it

Expose the class through the `usmp.connectors` entry point in your package's `pyproject.toml`:

```toml
[project.entry-points."usmp.connectors"]
acme = "usmp_acme:AcmeConnector"
```

After `pip install usmp-acme`, it appears in `usmp connectors list` and works with
`usmp migrate --from acme:account=me --to …`.

## 4. Prove it with the conformance kit

```python
# tests/test_conformance.py
from usmp.testing import ConnectorConformance


class TestAcme(ConnectorConformance):
    domain = "calendar"

    def make_connector(self):
        return AcmeConnector({"account": "usmp-test"})

    def sample_items(self):
        return [...]  # canonical events your connector should write

    def provider_state(self):
        return snapshot_of_test_account()  # optional, proves reading changes nothing
```

`pytest` then runs four checks:

| Check | What it proves |
|---|---|
| `test_capabilities` | The manifest is valid and names your connector. |
| `test_reader_is_read_only` | The reader is a `Reader`, not a `Writer`, and exposes no mutating methods. |
| `test_read_follows_contract` | Items are in the right domain, use only declared fields, are unique, come parents-first, and reading leaves the provider unchanged. |
| `test_write_read_back_round_trip` | Every field you declare as both writable and readable survives a write and read-back unchanged. |

Read-only or write-only connectors set `can_write = False` or `can_read = False`. The individual
checks (`check_read`, `check_round_trip`, …) can also be called directly.

## 5. Safe re-runs

People run migrations twice, and connections drop halfway. USMP keeps a progress record next to
the backup and passes it to your writer as `known` (usmp_id → `WriteResult`). The default
`already_written` trusts that record, which already makes `usmp migrate --resume` safe.

To also make a plain second run safe, store each item's `usmp_id` on the provider, somewhere that
is not part of the item itself (an extended property, a hidden field, `X-USMP-ID` in iCalendar),
and override `already_written` to look items up by it. Remember where already-present parents
are, so that `write()` can put new children under them. Then set `finds_written = True` in your
conformance test class to run `test_rerun_finds_written_items`.

## 6. Retries and rate limits

`usmp.sdk.retry` retries the requests you tell it are worth retrying, with exponential back-off,
full jitter, the server's `Retry-After`, and limits on attempts and total waiting time:

```python
from usmp.sdk.retry import TransientError, parse_retry_after, retrying


def get(session, url):
    def attempt():
        response = session.get(url)
        if response.status_code in (429, 503):
            wait = parse_retry_after(response.headers.get("Retry-After"))
            raise TransientError(f"busy (HTTP {response.status_code})", retry_after=wait)
        return response

    return retrying(attempt, what="reading the calendar list")
```

Only you know which requests are safe to send twice. Reads are; so is a `PUT` of the same content
to the same place, and a create the provider refuses to repeat (a conditional `PUT`, or an import
keyed by UID). For any other create, retry only on answers that mean "not done" (429), or pass
`before_retry=` a function that looks for your own earlier write by `usmp_id` and returns it if
found. When a retried create is refused as a duplicate, check whether the duplicate is yours
before reporting a conflict (spec §8.1).

In tests, give your client a `RetryPolicy(sleep=lambda _seconds: None)` so retries happen at once.
