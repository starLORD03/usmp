# USMP — Universal Service Migration Protocol

**Move your data between services and platforms without losing it.**

USMP is an open, vendor-neutral protocol and a reference implementation for migrating a user's data
and service configuration from one provider to another: Google Calendar to iCloud, AWS to Azure, one
car's driver profile to the next. You switch services; your data comes with you, and you get proof
that nothing was lost.

> **Status: early development (Phase 0).** Nothing is usable yet. Follow the
> [roadmap](docs/plan/ROADMAP.md) and [milestones](https://github.com/starLORD03/usmp/milestones).
> The first usable release will migrate calendars (Google, iCloud, CalDAV/Nextcloud, `.ics`).

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

## Planned usage (Phase 2)

```console
$ pipx install usmp-protocol
$ usmp migrate            # interactive wizard: pick source, target, preview, migrate, verify
$ usmp migrate --from google --to caldav --dry-run
```

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
