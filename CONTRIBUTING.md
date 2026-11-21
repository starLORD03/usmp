# Contributing to USMP

Thanks for helping. This guide covers how work flows through the repo.

## Ways to contribute

- **Report a bug or request a feature:** open an issue using one of the templates.
- **Request or build a connector** for a provider USMP doesn't support yet. Use the *Connector
  request* template.
- **Improve the spec or docs.** Changes to the protocol itself need an ADR (see below).

## Workflow

USMP uses trunk-based development ([ADR 0006](docs/adr/0006-development-workflow.md)).

1. Open or pick an issue. Larger work belongs to a phase milestone.
2. Create a short-lived branch from `main`:
   `feat/…`, `fix/…`, `docs/…`, `ci/…`, `chore/…`, `refactor/…`, `test/…`.
3. Write tests first, then the code.
4. Open a pull request. The **PR title must follow [Conventional Commits](https://www.conventionalcommits.org/)**,
   for example `feat(calendar): add CalDAV reader`, because it becomes the squash-merge commit and
   drives the changelog and version.
5. All required CI checks must pass. A maintainer reviews and squash-merges.

| Title prefix | Meaning | Version bump |
|---|---|---|
| `feat:` | New user-facing capability | minor |
| `fix:` | Bug fix | patch |
| `feat!:` or `BREAKING CHANGE:` | Incompatible change | major (minor while `0.x`) |
| `docs:` `test:` `ci:` `chore:` `refactor:` `perf:` `build:` | Everything else | none |

## Development setup

Instructions arrive with the Python scaffold (Phase 0.2). It will use [uv](https://docs.astral.sh/uv/).

## Architecture decisions

Significant decisions are recorded as ADRs in [`docs/adr/`](docs/adr/). To propose one, copy the
format of an existing ADR, number it next in sequence, set `Status: Proposed`, and open a PR.

## Code of conduct

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
