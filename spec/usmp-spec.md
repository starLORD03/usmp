# USMP Specification — Core

| | |
|---|---|
| Status | **Draft 0.1** (Phase 1). Breaking changes are possible until spec 1.0. |
| Schemas | [`schemas/core/`](schemas/core/) (JSON Schema 2020-12, generated from the reference implementation) |
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
Bundles contain personal data: tools SHOULD store them only where the user chooses and MUST NOT
write credentials into them.

## 8. Migration and verification

A migration of one domain from a source to a target proceeds as follows:

1. **Plan.** Check that the source can read and the target can write the domain.
2. **Export.** Read all items from the source **using only the Reader role**, and write a bundle.
   Implementations MUST NOT modify the source in any way.
3. **Preview.** Compute losses (§6) from the item payloads and both capability manifests. Present
   counts and losses to the user.
4. **Confirm.** Do not write to the target without the user's confirmation, which MAY be given in
   advance (e.g. a `--yes` flag). A *dry run* stops here.
5. **Import.** Write items to the target. The writer reports the target's native id for each item.
6. **Read back** the written items from the target.
7. **Verify.** For each item, the **comparable fields** are those present in the source payload
   that the target can both write and read. Compare projected hashes over the comparable fields.

| Status | Condition |
|---|---|
| `verified` | Projected hashes match and no losses were declared for the item. |
| `verified_with_declared_loss` | Projected hashes match; some fields were declared lost. |
| `mismatch` | Projected hashes differ. The report lists the differing fields. |
| `missing` | The item could not be read back from the target. |

Schema for the report: [`verification-report.schema.json`](schemas/core/verification-report.schema.json).
A migration is **ok** only if no item is `mismatch` or `missing`. Tools SHOULD exit with a non-zero
status otherwise.

## 9. Versioning

* The core envelope, manifest and report formats carry their own version fields
  (`format_version`, `report_version`). Readers MUST reject versions they do not support.
* Domain schemas are versioned by integer major version. Adding an optional field is not a breaking
  change; renaming, removing or changing the meaning of a field is, and needs a new major version.
