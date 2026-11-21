# ADR 0005: Calendar as the first vertical

- Status: Accepted
- Date: 2026-11-22
- Deciders: starLORD03

## Context

The generic core needs one real domain to prove it. That domain should have open standards, strong user value and testable providers.

## Decision

The calendar domain ships first, with connectors for ICS files, CalDAV (iCloud, Nextcloud, Fastmail, Radicale) and Google Calendar. The canonical model is based on JSCalendar (RFC 8984) and aligned with iCalendar (RFC 5545).

## Consequences

- Covers the motivating case: Google → iOS (iCloud) or Linux (CalDAV).
- Recurrence and time zones are hard; property-based tests are required.
- CalDAV can be integration-tested in CI with Radicale (pure Python, no Docker).
