# ADR 0004: Canonical model, connectors, bundle and verification

- Status: Accepted
- Date: 2026-11-22
- Deciders: starLORD03

## Context

Direct provider-to-provider converters scale as N². Users need proof that data is intact, and the source must never be harmed.

## Decision

- Each domain has one versioned canonical schema (for example `usmp.calendar/v1`).
- Connectors map provider data to and from the canonical form and declare a capability manifest.
- Source connectors are bound only through a read-only `Reader` interface (non-destructive by construction), and a conformance test guards this.
- Every run writes a `.usmp` bundle (manifest, items, raw originals, hashes, losses). The bundle is both a backup and an offline transfer format.
- After import the engine reads back from the target and verifies hashes over the comparable fields. Unmappable fields are reported as declared losses, before and after the run.

## Consequences

- N connectors instead of N² converters.
- Canonical schemas need careful design and versioning (spec work in Phases 1–2).
- Verification requires target connectors to support read-back.
