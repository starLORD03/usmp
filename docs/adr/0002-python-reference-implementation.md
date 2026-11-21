# ADR 0002: Python for the reference implementation

- Status: Proposed
- Date: 2026-11-22
- Deciders: starLORD03

## Context

We need a language that is readable for contributors, cross-platform, and has mature client libraries for iCalendar, CalDAV, Google, Microsoft, AWS and Azure.

## Decision

The reference engine, SDK, connectors and CLI use Python ≥ 3.11, managed with `uv`, typed with mypy --strict and models in Pydantic v2. The protocol itself (`spec/`) is language-neutral: Markdown plus JSON Schema.

## Consequences

- Fast iteration and an easy on-ramp for connector authors.
- A single static binary is harder; Phase 3 adds PyInstaller builds and pipx/Homebrew distribution.
- Embedded/in-vehicle runtimes may need a Rust or Go bundle reader later; the language-neutral bundle format makes that possible.
