# USMP Domain: Calendar (`calendar`, schema version 1)

| | |
|---|---|
| Status | **Draft** (Phase 2) |
| Schemas | [`calendar.schema.json`](../schemas/domains/calendar/v1/calendar.schema.json), [`event.schema.json`](../schemas/domains/calendar/v1/event.schema.json) |
| Based on | [RFC 5545 iCalendar](https://www.rfc-editor.org/rfc/rfc5545), [RFC 8984 JSCalendar](https://www.rfc-editor.org/rfc/rfc8984) |

Key words follow [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119), as in the [core spec](../usmp-spec.md).

## 1. Item types

| Type | Meaning | `parent` |
|---|---|---|
| `calendar` | A collection of events. | none |
| `event` | One event, or one overridden occurrence of a recurring event. | the calendar's `usmp_id` |

## 2. `calendar` fields

| Field | Type | Notes |
|---|---|---|
| `name` | string, required | Display name. |
| `description` | string | |
| `color` | `#rrggbb` | Lower-case hex. |
| `time_zone` | IANA zone | Default zone for new events. |

## 3. `event` fields

| Field | Type | Notes |
|---|---|---|
| `uid` | string, required | iCalendar `UID`. Shared by a recurring event and its overridden occurrences. |
| `title` | string | `SUMMARY` |
| `description` | string | |
| `location` | string | |
| `url` | string | |
| `start` | date or local date-time, required | See §4. |
| `end` | same form as `start` | **Exclusive.** Absent means the provider default (one day for all-day events, zero length otherwise). |
| `time_zone` | IANA zone | Zone of `start`, and of `end` unless `end_time_zone` is set. Absent for all-day events and floating times. |
| `end_time_zone` | IANA zone | Only when the end is in a different zone. |
| `status` | `confirmed` · `tentative` · `cancelled` | |
| `availability` | `busy` · `free` | iCalendar `TRANSP` |
| `privacy` | `public` · `private` · `confidential` | iCalendar `CLASS` |
| `recurrence_rules` | list of RRULE strings | §5 |
| `recurrence_dates` | list, same form as `start` | `RDATE` |
| `excluded_dates` | list, same form as `start` | `EXDATE` |
| `recurrence_id` | same form as `start` | Set on an overridden occurrence (§5). |
| `organizer` | `{email, name?}` | |
| `attendees` | list of `{email, name?, role?, status?}` | `role`: `chair` · `required` (default) · `optional` · `non_participant`. `status`: `needs_action` (default) · `accepted` · `declined` · `tentative` · `delegated`. |
| `alarms` | list of `{trigger, action?}` | `trigger` is an ISO 8601 duration relative to `start` (e.g. `-PT15M`). `action`: `display` (default) · `email` · `audio`. |
| `categories` | list of strings | |
| `attachments` | list of `{uri, media_type?, title?}` | References only: files are not copied. |
| `color` | `#rrggbb` | |

Creation and modification timestamps are deliberately **not** part of the model: every target sets
its own, so they could never be verified.

## 4. Times

* An **all-day** event has `start` as a date (`YYYY-MM-DD`) and no time zone. `end`,
  `recurrence_dates`, `excluded_dates` and `recurrence_id` MUST then also be dates.
* A **timed** event has `start` as a local date-time (`YYYY-MM-DDTHH:MM:SS`) **without an offset**.
  The zone is given separately in `time_zone`. Without `time_zone` the time is *floating* (the
  same wall-clock time in every zone).
* Times in UTC use `time_zone: "UTC"`. The aliases `Etc/UTC`, `GMT`, `Etc/GMT`, `Z` and similar are
  normalised to `UTC`.
* When `end_time_zone` is absent or equal to `time_zone`, `end` MUST be after `start`.

## 5. Recurrence

A recurring event is **one** item with `recurrence_rules`, plus optional `recurrence_dates` and
`excluded_dates`. RRULE strings use RFC 5545 syntax without the `RRULE:` prefix.

An occurrence whose details were changed ("edit this occurrence only") is a **separate** `event`
item with the same `uid`, a `recurrence_id` equal to the original start of the occurrence it
replaces, and no `recurrence_rules`. Connectors MUST preserve this pairing.

## 6. Canonical form

To make content hashes comparable across providers (core spec §4), payloads MUST be in canonical
form. The reference implementation's `usmp.domains.calendar.canonicalize()` produces it:

1. Fields that are absent, `null`, empty strings, empty lists or equal to their default are
   **omitted**.
2. Strings are trimmed.
3. RRULEs are upper-case, with `FREQ` first and the other parts sorted by name
   (`FREQ=WEEKLY;BYDAY=MO;INTERVAL=2`).
4. Email addresses are lower-case without `mailto:`.
5. Colours are lower-case `#rrggbb`. Durations are upper-case without a leading `+`.
6. `categories`, `recurrence_dates` and `excluded_dates` are sorted and de-duplicated.
   `attendees` are sorted by email (emails MUST be unique), `alarms` by trigger then action, and
   `attachments` by URI.

Implementations MUST reject items of this domain whose payload is invalid or not canonical.

## 7. Mapping notes for connectors

* Properties the model cannot hold (e.g. provider-specific colours by id, conference links,
  `X-` properties) MUST be reported as `unsupported_by_canonical` losses at export. Connectors
  SHOULD keep the original in the bundle's `raw/` entry and MAY keep it in `extensions`.
* A provider that cannot store a field (e.g. no per-event colour) declares it absent from
  `write_fields`; USMP then reports the loss before migrating.
