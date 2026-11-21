# ADR 0003: Local-first runtime

- Status: Proposed
- Date: 2026-11-22
- Deciders: starLORD03

## Context

Migrations handle personal data and provider credentials. A hosted gateway would make the project a custodian of that data, with a heavy compliance burden.

## Decision

Migrations run on the user's machine through a CLI/library. Credentials live in the OS keychain. A self-hostable server (`usmp serve`) that reuses the same engine comes in Phase 5. There is no project-operated hosted service.

## Consequences

- Strong privacy story, no data custody.
- Users must install a tool; the CLI wizard and later the GUI must make this easy.
- Enterprise/remote scenarios wait until Phase 5.
