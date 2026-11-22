# USMP — Universal Service Migration Protocol

**Move your data between services and platforms without losing it.**

USMP is an open, vendor-neutral protocol and a reference implementation for migrating a user's data
and service configuration from one provider to another: Google Calendar to iCloud, AWS to Azure, one
car's driver profile to the next. You switch services; your data comes with you, and you get proof
that nothing was lost.

> **Status: beta for calendars (Phase 2).** Google Calendar, iCloud, Nextcloud, Fastmail, other
> CalDAV servers and `.ics` files work today. Follow the [roadmap](docs/plan/ROADMAP.md) and
> [milestones](https://github.com/starLORD03/usmp/milestones).

## How it works

```
Source ──read only──► canonical data ──► .usmp bundle (backup + hashes) ──► target ──read back──► verification report
```

- **Platform-independent core.** Each provider is handled by a small plugin called a *connector*.
  The engine knows nothing about Google, Apple, AWS or anyone else.
- **One canonical model per domain** (calendar, contacts, …), published as versioned JSON Schema, so
  connectors can be written in any language.
- **Your source is never modified.** Connectors can only read from the source.
- **Nothing is lost silently.** Fields the target can't hold are listed *before* you migrate.
  Afterwards, USMP reads everything back and verifies it.
- **Local-first.** Runs on your machine. Your data and credentials stay with you.

Read the [architecture overview](docs/architecture/overview.md) for details.

## Quick start

```console
$ pipx install 'usmp-protocol[google]'    # drop [google] if you don't use Google Calendar
$ usmp migrate
```

`usmp migrate` with no options walks you through it:

1. **Where is your calendar now?** Google, iCloud, Nextcloud, Fastmail, another CalDAV server or a
   `.ics` file. It asks for the account and signs you in (passwords go to your system keychain).
2. **Where should it go?** The same choices.
3. **Preview.** You see how many calendars and events will move and anything that can't be
   carried over, before anything is written. A backup of the source is saved first.
4. **Migrate and verify.** Everything is read back from the target and checked. You get a report.

At the end it prints the equivalent command, so you can repeat or script it:

```console
$ usmp migrate --from google:account=you@gmail.com \
    --to caldav:url=https://caldav.icloud.com,username=you@icloud.com --dry-run
```

Meeting links (Google Meet, Zoom, Teams), rooms, reminders, recurring events with exceptions and
time zones all carry over. See the guides for [Google](docs/guides/google.md) and
[CalDAV](docs/guides/caldav.md).

## Repository layout

| Path | Contents |
|---|---|
| [`spec/`](spec/usmp-spec.md) | The protocol specification and JSON Schemas |
| [`src/usmp/`](src/usmp/) | Python reference implementation |
| [`docs/architecture/`](docs/architecture/) | Architecture overview |
| [`docs/adr/`](docs/adr/) | Architecture decision records |
| [`docs/plan/ROADMAP.md`](docs/plan/ROADMAP.md) | Phased roadmap with exit criteria |
| [`docs/vision/v2/`](docs/vision/v2/) | The original v2.0 design (OS-level app migration, business model) |

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). To report a security issue, see
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
