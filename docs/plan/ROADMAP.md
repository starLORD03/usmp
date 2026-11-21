# USMP Development Roadmap (v3)

> Status: **Draft for review** — Phase 0 planning PR.
> Each **phase** = a GitHub milestone and a minor release. Each **stage** = one or more issues and
> PRs. A phase is done only when its exit criteria pass in CI.

## Decisions this plan is based on

| Topic | Decision | ADR |
|---|---|---|
| Positioning | Open protocol + reference implementation; marketplace/business model becomes future vision | [0001](../adr/0001-open-protocol-first.md) |
| Language | Python ≥ 3.11 for the reference engine/SDK; spec is language-neutral JSON Schema | [0002](../adr/0002-python-reference-implementation.md) |
| Runtime | Local-first CLI/library; optional self-hosted server later | [0003](../adr/0003-local-first.md) |
| Core architecture | Canonical model per domain + connectors + `.usmp` bundle + verification | [0004](../adr/0004-canonical-model-and-bundle.md) |
| First vertical | Calendar (ICS, CalDAV/iCloud/Nextcloud, Google) | [0005](../adr/0005-calendar-first-vertical.md) |
| Process | Trunk-based, PRs merged by maintainer, conventional commits, ECC workflow | [0006](../adr/0006-development-workflow.md) |
| MVP guarantees | Non-destructive source + verification report | [0004](../adr/0004-canonical-model-and-bundle.md) |
| First users | End users (CLI wizard) and developers (connector SDK) | — |

---

## Phase 0 — Foundation and process · `v0.0.x`

**Goal:** a clean repo, a working CI/CD pipeline and a defined way of working, before any feature code.

| Stage | Deliverables |
|---|---|
| 0.1 Repo restructure | Move v2 docs to `docs/vision/v2/`; new README; CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, CODEOWNERS; issue and PR templates; labels; milestones for Phases 0–7 |
| 0.2 Python scaffold | `uv` project with `src/` layout, `pyproject.toml` (distribution `usmp-protocol`, import `usmp`, CLI `usmp`), ruff, mypy `--strict`, pytest + coverage, pre-commit; `usmp --version` works |
| 0.3 CI | GitHub Actions: lint, type-check, test matrix (Ubuntu/macOS/Windows × Python 3.11–3.14), coverage gate, commitlint on PR titles, CodeQL, gitleaks, pip-audit, Dependabot |
| 0.4 CD | release-please (changelog + version + tag), PyPI trusted publishing (TestPyPI first), MkDocs Material docs site on GitHub Pages |
| 0.5 Agent tooling | `CLAUDE.md`/`AGENTS.md` with the ECC workflow; graphify and code-review-graph configs; branch protection on `main` (required checks, squash merge) |

**Exit criteria:** a trivial PR goes green on every check; merging a `feat:` commit produces a release PR; docs site is live.

## Phase 1 — Protocol core and engine · `v0.1.0`

**Goal:** a generic engine that can migrate *any* domain, proven with an in-memory demo connector.

| Stage | Deliverables |
|---|---|
| 1.1 Spec draft | `spec/usmp-spec.md`: concepts, envelope, capability manifest, bundle format, loss records, verification semantics; JSON Schemas under `spec/schemas/core/` |
| 1.2 Core models | Pydantic v2 models; JSON Schemas generated from them; CI check that fails if committed schemas drift |
| 1.3 Bundle | Read/write `.usmp` (zip + manifest + JSONL + sha256 hashes); `usmp bundle inspect` and `usmp bundle validate` |
| 1.4 Connector SDK | `Reader`/`Writer` protocols, capability manifest, entry-point registry, `usmp connectors list` |
| 1.5 Engine + verifier | Pipeline (plan → export → snapshot → preview → transform → import → verify → report); reports in JSON, Markdown and HTML; `memory` demo connector |
| 1.6 Conformance kit v0 | `usmp.testing` pytest plugin: capability-manifest checks, round-trip checks, a read-only source guard |

**Exit criteria:** an end-to-end demo migration passes; source is provably untouched (guard test); coverage ≥ 85 %.

## Phase 2 — Calendar MVP · `v0.2.0` → `v0.4.0`

**Goal:** a real person can move their calendar from Google to iCloud or Nextcloud and see proof that nothing was lost.

| Stage | Deliverables |
|---|---|
| 2.1 Calendar model | `usmp.calendar/v1`, based on JSCalendar (RFC 8984) and aligned with iCalendar (RFC 5545): events, recurrence and exceptions, time zones, attendees, alarms, attachment references, calendars as collections |
| 2.2 ICS connector | Read/write `.ics`; property-based round-trip tests (Hypothesis); Apple Calendar and Thunderbird import/export work through it |
| 2.3 CalDAV connector | Read/write; works with iCloud (app-specific password), Nextcloud, Fastmail and Radicale; CI integration tests against a Radicale server |
| 2.4 Google connector | Google Calendar API with OAuth PKCE and tokens in the keychain; recorded-HTTP tests in CI; manual end-to-end checklist against a real account |
| 2.5 CLI wizard | `usmp migrate`: choose source → sign in → preview (counts, date range, declared losses) → dry-run → run → verify → report. Plain-language errors. Also fully scriptable with flags |
| 2.6 Release | Docs: "Google → iCloud in 5 minutes", "Write your own connector"; first public beta on PyPI |

**Exit criteria:** Google → iCloud, Google → CalDAV and ICS → CalDAV all migrate a 1,000-event test calendar (recurring events, exceptions, multiple time zones) with 0 `mismatch` and 0 `missing`.

## Phase 3 — Robustness · `v0.5.0`

Resumable runs (checkpoints), idempotent re-runs (`usmp_id` stored on the target), rollback (delete only what USMP created), selective migration (date ranges, calendars), retries and rate limiting, encrypted bundles, threat model and security review, Microsoft Graph (Outlook) connector, distribution via pipx, a Homebrew tap and standalone binaries.

## Phase 4 — Second and third domains · `v0.6.0`–`v0.7.0`

Contacts (vCard, CardDAV, Google People) and Tasks (VTODO, Google Tasks). **This proves generality: the core engine must need no domain-specific changes.** Spec reaches `1.0-rc`.

## Phase 5 — Local server and GUI · `v0.8.0`

`usmp serve` (FastAPI, OpenAPI 3.1) on the same engine; a simple local web UI over it; optional continuous "bridge" sync mode.

## Phase 6 — New domain families · `v1.x`

Each one starts with a spec and canonical model, then reference connectors:

- **Desktop app settings (OS ↔ OS):** VS Code settings and extensions, dotfiles, file associations. This is the original v2 vision.
- **Media and streaming:** playlists and libraries using open formats (XSPF/M3U) and ISRC matching.
- **Cloud infrastructure (AWS ↔ Azure):** infra-neutral model; read through cloud SDKs; emit OpenTofu for the target; S3 ↔ Blob data mover.
- **Vehicle driver profiles:** canonical profile model and bundle; connectors as OEM or Android Automotive access allows.

## Phase 7 — Ecosystem and governance · spec `1.0`

Spec 1.0 freeze and a versioning policy; a community connector registry (an index, not a store); certification through the conformance kit; governance document; bundle readers in other languages (TypeScript/Go).

---

## How each stage is built

1. **Issue.** `/plan-prd` for the phase, then `/epic-decompose` into stage issues with acceptance criteria.
2. **Plan.** `/plan` on a branch named `feat/<stage>-<slug>`.
3. **Build test-first.** Write tests first, then implement, until green (`/orch-add-feature` for larger features).
4. **Review.** `/python-review` and `/code-review`, then code-review-graph `impact` to check blast radius, then `/quality-gate`.
5. **PR.** Conventional-commit title. All CI checks must pass. **The maintainer (starLORD03) reviews and merges** with a squash merge.
6. **Release.** release-please collects merged `feat:` and `fix:` commits into a release PR. Merging it tags the version and publishes to PyPI and the docs site.
7. **Graph refresh.** Rebuild graphify / code-review-graph after a merge to keep the architecture map current.

## Open items for the maintainer

- [ ] Grant the `workflow` scope to the starLORD03 token (needed to push CI files).
- [ ] Confirm the PyPI distribution name `usmp-protocol` (`usmp` is taken).
- [ ] Phase 2.4: create a Google Cloud OAuth client (desktop app) for the Google connector.
- [ ] Phase 2.3: an iCloud app-specific password for manual end-to-end tests (never committed).
