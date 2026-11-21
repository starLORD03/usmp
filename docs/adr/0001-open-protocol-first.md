# ADR 0001: Open protocol first

- Status: Accepted
- Date: 2026-11-22
- Deciders: starLORD03

## Context

The v2.0 docs describe a marketplace with a revenue split and focus only on OS-level apps. The goal has widened to any service: calendars, streaming, vehicles, cloud. A marketplace needs adoption that does not exist yet.

## Decision

USMP is positioned as an open, vendor-neutral protocol (spec + JSON Schemas) with an MIT-licensed reference implementation. The v2.0 business model and OS-centric docs move to `docs/vision/v2/` as future vision.

## Consequences

- Adoption is easier: anyone can implement the spec.
- Business-model work is deferred; nothing in the protocol prevents it later.
- The existing docs are preserved, not deleted.
