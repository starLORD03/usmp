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
| `attendees` | list of `{email, name?, kind?, role?, status?}` | `kind` (iCalendar `CUTYPE`): `individual` (default) · `group` · `resource` · `room` · `unknown`. `role`: `chair` · `required` (default) · `optional` · `non_participant`. `status`: `needs_action` (default) · `accepted` · `declined` · `tentative` · `delegated`. |
| `alarms` | list of `{trigger, action?}` | `trigger` is an ISO 8601 duration relative to `start` (e.g. `-PT15M`). `action`: `display` (default) · `email` · `audio`. |
| `categories` | list of strings | |
| `attachments` | list of `{uri, media_type?, title?}` | References only: files are not copied. |
| `color` | `#rrggbb` | |
| `conferences` | list of `{uri, label?, features?}` | Ways to join online or by phone (iCalendar `CONFERENCE`, RFC 7986). `features`: any of `audio` · `chat` · `feed` · `moderator` · `phone` · `screen` · `video`. |
| `provider_data` | object: connector name → object | Provider-specific details with no place in the model (§7), so the event can be restored exactly on the same provider. Values are opaque JSON. |

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
* When `end_time_zone` is absent or equal to `time_zone`, `end` MUST be after `start`. Readers
  that meet a provider event ending before it starts (it happens in exports) SHOULD read it
  without `end` and declare a `value_converted` loss on `end`, instead of failing the whole
  export; the original stays in the bundle's raw data.

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
5. Colours are lower-case `#rrggbb`. Durations use days and time only, largest units first, no
   leading `+` and no weeks (`P1W` → `P7D`, `PT90M` → `PT1H30M`, zero → `PT0S`).
6. `categories`, `recurrence_dates` and `excluded_dates` are sorted and de-duplicated.
   `attendees` are sorted by email (emails MUST be unique), `alarms` by trigger then action,
   `attachments` by URI, `conferences` by URI (duplicates removed, `features` sorted) and
   `provider_data` by connector name, with empty entries removed.

Implementations MUST reject items of this domain whose payload is invalid or not canonical.

## 7. Mapping notes for connectors

* Provider-specific properties that the model cannot express but the same provider could take
  back (e.g. Google colour ids, event types, app data) SHOULD be kept in
  `provider_data.<connector>`. They are part of the payload, so they are hashed and verified.
  A target that cannot store another provider's entry MUST declare it as a loss on import.
* Anything else the model cannot hold MUST be reported as an `unsupported_by_canonical` loss at
  export. Connectors SHOULD keep the original in the bundle's `raw/` entry.
* **iCalendar mapping.** `conferences` ↔ `CONFERENCE` (`X-GOOGLE-CONFERENCE` and
  `X-MICROSOFT-SKYPETEAMSMEETINGURL` are also read as video conferences). `kind` ↔ `CUTYPE`.
  `provider_data` ↔ `X-USMP-PROVIDER-DATA`, the base64 of the canonical JSON. Because many apps
  ignore `CONFERENCE`, writers add the join links to the end of `DESCRIPTION`
  ("Join online: …"); readers remove exactly that text again, so the notes round-trip unchanged.
* A provider that cannot store a field (e.g. no per-event colour) declares it absent from
  `write_fields`; USMP then reports the loss before migrating.
* **Marking written items (core §8.1).** iCalendar writers add `X-USMP-ID:<usmp_id>` to each
  VEVENT they write, and `.ics` files carry the calendar's `X-USMP-ID` on the VCALENDAR. CalDAV
  writers also set the WebDAV property `{https://github.com/starLORD03/usmp}usmp-id` on calendars
  they create, where the server allows it. Google Calendar writers use the private extended
  property `usmp_id`. Readers MUST NOT put these markers into the payload or `provider_data`:
  they identify an item, they are not part of it.
* **Google's organizer rule.** Google Calendar only imports an event whose organizer or guests
  include the target calendar. When neither does (an invitation from another account, or from
  the same person's old address), the writer adds the calendar as an accepted guest and records
  its address in the private extended property `usmp_added_guest`. Readers leave that guest out,
  so the attendee list round-trips unchanged.
* **What Google changes, kept.** Google adds the names of booked rooms to the location, can only
  attach meetings it created, and stores rooms as resources. Writers keep the written location
  (`usmp_location`), the event's `conferences` as compact JSON (`usmp_conferences`, when the join
  links had to go into the notes) and the room addresses (`usmp_rooms`) as private extended
  properties, each only if it fits Google's 1,024-character limit (otherwise the change stays a
  declared loss). Readers restore the location, the conferences (removing exactly the join text
  added to the notes) and `kind: room`.

## 8. Selecting events

How a selection (core spec §8.2) applies to calendar items:

* **Calendars.** With `collections`, only calendars whose `name` matches one of them (ignoring
  case) are kept, even if none of their events is; a name that matches no calendar is an error.
  Without `collections`, every calendar that still holds a kept event is kept.
* **Events.** An event is in the date range when it starts before `until` and ends after `since`
  (an event with no length is in range when it starts at or after `since`). Without `end`, an
  all-day event lasts one day and a timed event has no length. Zoned times are compared as
  instants. All-day and floating times are compared by wall-clock time against the bound's own
  wall-clock time, so `since: 2026-01-01T00:00+01:00` keeps an all-day event on 1 January.
* **Series are never cut.** Events sharing a calendar and a `uid` (a master and its overridden
  occurrences) are kept or left out together. A series is kept when any of its members is in
  range, or when any occurrence produced by the master's `recurrence_rules` or
  `recurrence_dates` is. `excluded_dates` are not considered, so a series is never dropped because
  of them. An event whose times cannot be worked out is kept.

## 9. Data warnings

Tools SHOULD warn (core spec §8, step 3), without changing anything, about:

* an event whose `end` in `end_time_zone` falls before its `start` in `time_zone`, for example a
  flight whose arrival time was entered in the wrong zone;
* a timed event lasting more than 7 days. Long all-day events (holidays, trips) are normal.

A warning carries the event's `usmp_id`, the field (`end`), a plain description, and a hint for
finding the event: its start (`starts 2026-03-02T09:00:00 (Europe/Paris)`), never its title,
notes or attendees.
