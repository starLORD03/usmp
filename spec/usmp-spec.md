# USMP Specification — Core

| | |
|---|---|
| Status | **Draft 0.1** (Phase 1). Breaking changes are possible until spec 1.0. |
| Schemas | [`schemas/core/`](schemas/core/) (JSON Schema 2020-12, generated from the reference implementation) |
| Domains | [Calendar](domains/calendar.md) |
| Editors | starLORD03 |

The key words MUST, MUST NOT, REQUIRED, SHOULD, SHOULD NOT and MAY are to be interpreted as
described in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119).

## 1. Purpose and scope

USMP moves a user's data from a **source** provider to a **target** provider through a
provider-independent **canonical model**, and proves afterwards what arrived. This document
defines the parts every implementation shares: the item envelope, hashing, capability
manifests, loss records, the bundle format and verification. Each **domain** (calendar, contacts,
…) defines its own canonical payload schema in a separate document.

## 2. Terminology

| Term | Meaning |
|---|---|
| Domain | A category of data with one canonical schema, identified by a lower-case name (e.g. `calendar`) and an integer major `schema_version`. |
| Item type | A kind of record within a domain (e.g. `calendar` has `calendar` and `event`). |
| Item | One record: an envelope (§3) around a canonical payload. |
| Field | A **top-level** key of an item's payload. Capability manifests, losses and verification work at field granularity. |
| Connector | A plugin that maps one provider's data to and from canonical items. |
| Reader / Writer | The two connector roles. A connector acting as a source is used only as a Reader. |
| Bundle | A `.usmp` file: a snapshot of exported items (§7). |

## 3. Item envelope

Schema: [`item.schema.json`](schemas/core/item.schema.json).

| Field | Req. | Description |
|---|---|---|
| `usmp_id` | ✔ | Stable identifier (§3.1). |
| `domain` | ✔ | Domain name. |
| `type` | ✔ | Item type within the domain. |
| `schema_version` | ✔ | Major version of the domain schema the payload conforms to. |
| `source` | ✔ | `{connector, native_id}`: where the item was read. |
| `parent` | | `usmp_id` of the containing item (e.g. an event's calendar). |
| `payload` | ✔ | The canonical record. Top-level keys are fields. |
| `extensions` | | Provider-specific data, keyed by connector name. |
| `content_hash` | ✔ | §4. |

Envelopes MUST NOT contain properties other than those listed.

### 3.1 `usmp_id`

`usmp_id` is `usmp:` followed by a UUIDv5 whose namespace is
`uuid5(NAMESPACE_URL, "https://github.com/starLORD03/usmp")` and whose name is
`"<connector>:<native_id>"` for the place the item was **first** read. It MUST NOT change when the
item is migrated onward, so that repeated migrations can recognise the same item.

### 3.2 Extensions

`extensions` lets a connector keep data the canonical model cannot express, so that a later
migration back to the same provider can restore it. Extensions MUST NOT be included in the content
hash and MUST NOT be relied on by other connectors. Data that is only kept in an extension, and
not in the payload, MUST be reported as a loss with reason `unsupported_by_canonical`.

## 4. Canonical JSON and content hash

**Canonical JSON** is UTF-8 JSON with object keys sorted by code point, no insignificant
whitespace, and strings not `\u`-escaped. `NaN` and infinities are not allowed. Domain schemas
SHOULD avoid floating-point numbers and use strings for decimals, because number formatting
differs between languages.

`content_hash` = `"sha256:"` + hex SHA-256 of the canonical JSON of:

```json
{"domain": ..., "type": ..., "schema_version": ..., "payload": ...}
```

`source`, `parent` and `extensions` are excluded, so the same record read from two providers
hashes the same when nothing was lost. A reader MUST reject an item whose `content_hash` does not
match its contents.

The **projected hash** for a set of fields *F* is the content hash computed with `payload`
replaced by its subset of keys in *F*.

## 5. Capability manifest

Schema: [`capability-manifest.schema.json`](schemas/core/capability-manifest.schema.json).

Each connector publishes, per domain and item type, the fields it can **read** (`read_fields`) and
**write** (`write_fields`). Field lists MUST be sorted and free of duplicates. A connector MUST NOT
emit payload fields it does not declare in `read_fields`, and MUST NOT silently drop a field it
declares in `write_fields`.

A connector that stores canonical items verbatim MAY use the wildcard `*`: as a `type` it matches
any item type in the domain (an exact type entry takes precedence), and a field list of exactly
`["*"]` means every field. `*` MUST NOT be combined with other names in a field list.

## 6. Loss records

Schema: [`loss-record.schema.json`](schemas/core/loss-record.schema.json).

A loss record states that one field of one item will not arrive at the target, or will not arrive
unchanged. Implementations MUST NOT drop or alter data without producing one.

| Reason | Produced when |
|---|---|
| `unsupported_by_canonical` | At export: the source has data the canonical model cannot hold. |
| `unsupported_by_target` | At planning: the item has a field the target cannot write. |
| `not_readable_by_target` | At planning: the target can write the field but not read it back, so it cannot be verified. |
| `value_converted` | The value is kept but changes form (e.g. precision reduced). |

Losses MUST be shown to the user **before** any data is written to the target, and included in the
verification report.

## 7. Bundle format (`.usmp`)

Schema for the manifest: [`bundle-manifest.schema.json`](schemas/core/bundle-manifest.schema.json).

A bundle is a ZIP archive with exactly these entries:

| Entry | Req. | Content |
|---|---|---|
| `manifest.json` | ✔ | Bundle manifest. |
| `items.jsonl` | ✔ | One item envelope per line, as canonical JSON, sorted by `usmp_id`. |
| `losses.json` | ✔ | JSON array of loss records produced at export. |
| `raw/<name>` | | Original provider payloads, for lossless fallback. |

`manifest.items_sha256` is the hash of the exact bytes of `items.jsonl`. Readers MUST verify it and
every item's `content_hash`, and MUST reject entries with absolute paths, `..` segments, or names
outside the table above. Readers SHOULD enforce limits on entry sizes and compression ratios.
Bundles contain personal data: tools SHOULD store them only where the user chooses, readable by
the user only, and MUST NOT write credentials into them.

### 7.1 Encrypted bundles

A bundle MAY be encrypted with a passphrase. An encrypted bundle is a complete plain bundle (the
ZIP above, unchanged) encrypted as one [age v1](https://age-encryption.org/v1) file with a
single passphrase (`scrypt`) recipient. Encryption wraps the bundle; it does not change the plain
format, so existing readers keep working on plain bundles, and anyone with the passphrase can turn
an encrypted bundle back into a plain one with any age implementation (`age -d`).

* **Identification.** A reader MUST decide by the first bytes: `age-encryption.org/v1\n` means
  encrypted, a ZIP signature means plain. It MUST NOT infer encryption from the file name, and
  MUST NOT try to open an encrypted bundle without a passphrase supplied by the user (no empty or
  default passphrases, no guessing). A wrong passphrase or damaged file MUST be reported as such;
  nothing from it may be used.
* **After decryption** every rule of §7 applies to the plain bundle as usual.
* **Writers** MUST NOT write the plain bundle to disk while producing the encrypted one, and MUST
  NOT store, log or report the passphrase. They SHOULD refuse passphrases shorter than 8
  characters.
* **Side files.** A progress record (§8.1) kept next to an encrypted bundle MUST NOT contain item
  content; loss `detail` texts, which can name people or calendars, are left out of it. Reports
  SHOULD say whether the backup is encrypted (`backup_encrypted`).

## 8. Migration and verification

A migration of one domain from a source to a target proceeds as follows:

1. **Plan.** Check that the source can read and the target can write the domain.
2. **Export.** Read all items from the source **using only the Reader role**, and write a bundle.
   Implementations MUST NOT modify the source in any way.
3. **Preview.** Compute losses (§6) from the item payloads and both capability manifests. Present
   counts and losses to the user, and any **data warnings**: valid items that look wrong in the
   source (domains define them; for calendars, events that end before they start). Warned items
   are still migrated unchanged; reports list the warnings under `warnings`, and they never
   affect whether a migration is ok.
4. **Confirm.** Do not write to the target without the user's confirmation, which MAY be given in
   advance (e.g. a `--yes` flag). A *dry run* stops here.
5. **Import.** Write items to the target. The writer reports the target's native id for each item.
6. **Read back** the written items from the target.
7. **Verify.** For each item, the **comparable fields** are those present in the source payload
   that the target can both write and read, minus any field the target reported as changed or
   lost when writing that item. Compare projected hashes over the comparable fields.

| Status | Condition |
|---|---|
| `verified` | Projected hashes match and no losses were declared for the item. |
| `verified_with_declared_loss` | Projected hashes match; some fields were declared lost. |
| `mismatch` | Projected hashes differ. The report lists the differing fields. |
| `missing` | The item could not be read back from the target. |

Schema for the report: [`verification-report.schema.json`](schemas/core/verification-report.schema.json).
A migration is **ok** only if no item is `mismatch` or `missing` and the report lists no `errors`
(problems that stopped part of the migration, such as a failed write). Tools SHOULD exit with a non-zero
status otherwise.

### 8.1 Re-runs and resume

Running the same migration again, or finishing one that stopped part-way, MUST NOT write any item
twice or change items already on the target.

* Writers SHOULD store each written item's `usmp_id` on the target, in a place that is not part of
  the item's canonical payload (for iCalendar, the `X-USMP-ID` property; see the domain specs).
  Before importing, a writer reports the items it finds there as **already present**; only the
  rest are written. Already-present items are still read back and verified like any other, so a
  copy someone edited on the target since is reported as `mismatch`, never overwritten.
* Tools MAY keep a progress record of written items next to the bundle, and MAY resume a
  migration from the bundle alone, without reading the source again. A progress record MUST NOT
  contain credentials. It is not part of the bundle (§7), which stays an unchanged snapshot.
* Report items carry `already_present: true` when they were not written by this run, and the
  summary counts them in `already_present`. Both are informational: they do not change an item's
  status or whether the migration is ok.
* Writers MAY retry requests that fail for a moment (rate limits, busy servers, dropped
  connections). A request whose earlier try may have been carried out without the answer
  arriving MUST only be repeated if repeating it cannot write the item twice (a conditional
  create such as `If-None-Match: *`, or an import keyed by UID), or after the writer has looked
  for its own earlier write by `usmp_id`. A conflict caused by the writer's own earlier try is
  that item's successful write, not an error.

### 8.2 Selecting part of the data

A migration MAY move only part of what the source exported, described by a **selection**
(`Selection` in `verification-report.schema.json`): an optional `since` and `until` instant and an
optional list of collection names (`collections`; calendars in the calendar domain).

* The bundle still holds everything that was exported. The selection is applied afterwards, so
  the same backup can later be imported in full or with another selection.
* Which items a selection keeps is defined by each domain spec (calendar: §8 of
  [`domains/calendar.md`](domains/calendar.md)). A domain without such rules MUST refuse a
  selection rather than guess. Domain rules MUST keep related items together (for calendars, a
  recurring series with all its overridden occurrences) and SHOULD err on the side of keeping an
  item when its position cannot be determined.
* Items left out are neither written nor verified, and MUST NOT be reported as `missing`. The
  report records the `selection` and counts the items it left out in `summary.left_out`, which is
  not included in `summary.total`. A report with `left_out` above zero MUST carry a `selection`.
* A resumed migration (§8.1) keeps the selection it started with.

## 9. Versioning

* The core envelope, manifest and report formats carry their own version fields
  (`format_version`, `report_version`). Readers MUST reject versions they do not support.
* Domain schemas are versioned by integer major version. Adding an optional field is not a breaking
  change; renaming, removing or changing the meaning of a field is, and needs a new major version.
