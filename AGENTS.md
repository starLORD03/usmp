# AGENTS.md — working on USMP

Instructions for AI coding agents (Claude Code, Codex, Cursor, …) and humans. Read
[`docs/architecture/overview.md`](docs/architecture/overview.md) and
[`docs/plan/ROADMAP.md`](docs/plan/ROADMAP.md) before starting a stage.

## Project in one paragraph

USMP is an open protocol plus a Python reference implementation for migrating user data between
providers (calendar first). The core engine is platform-independent. Providers are **connectors**
that map to and from a versioned **canonical model** per domain. Every run writes a `.usmp`
bundle and ends with a **verification report**.

## Non-negotiable rules

1. **Never write to a source.** Source connectors are used only through `Reader`. Do not add
   any code path from the engine to a source writer.
2. **Never lose data silently.** A field that can't be mapped must produce a `LossRecord`.
3. **No secrets or personal data** in code, fixtures, logs, bundles committed to git, or error
   messages. Test fixtures use synthetic data only.
4. **The core stays platform-neutral.** Nothing under `src/usmp/core/` or `src/usmp/sdk/` may
   import a provider SDK.
5. **Schemas are the contract.** If a model in `core/` or `domains/` changes, regenerate
   `spec/schemas/` in the same PR. Breaking a published schema needs a new schema version and an ADR.
6. **Never push to `main`.** Work on a branch and open a PR. The maintainer merges.

## Per-stage workflow (ECC)

Each roadmap stage has an issue in its phase milestone.

| Step | What to do | ECC helpers |
|---|---|---|
| 1. Understand | Read the issue, the relevant ADRs and the code graph | `/plan-prd` (phase level), code-review-graph `architecture` |
| 2. Plan | Restate the acceptance criteria and write a step plan; wait for approval on anything non-trivial | `/plan` |
| 3. Build test-first | Write a failing test, implement until green, refactor | `/orch-add-feature`, `/tdd` |
| 4. Review | Self-review the diff; check blast radius | `/python-review`, `/code-review`, code-review-graph `impact` |
| 5. Gate | Everything CI runs must pass locally | `/quality-gate` |
| 6. PR | Conventional Commit title, `Closes #N`, filled template | `/prp-pr` |

## Commands

```console
uv sync                                  # set up
uv run pre-commit run --all-files        # lint, format, secrets, mypy
uv run pytest --cov                      # tests + 85 % coverage gate
uv run zensical serve                    # docs preview (needs: uv sync --group docs)
```

## Conventions

- Python ≥ 3.11, full type hints, `mypy --strict` clean, ruff-formatted, 100-char lines.
- Tests mirror the source tree under `tests/`. Use property-based tests (Hypothesis) for mappers
  and round-trips.
- Branches: `feat/…`, `fix/…`, `docs/…`, `ci/…`, `chore/…`, `refactor/…`, `test/…`.
- PR titles and commits follow Conventional Commits (`feat(calendar): …`). They drive releases.
- Significant decisions get an ADR in `docs/adr/`.

## Code graphs

- **code-review-graph**: structural graph for review and impact analysis. Build it with
  `code-review-graph build` and update with `code-review-graph update`. Its MCP server is configured
  in `.mcp.json`. Output in `.code-review-graph/` is git-ignored.
- **graphify**: knowledge graph across code *and* docs/spec. Run `/graphify` in Claude Code. Output
  in `graphify-out/` is git-ignored. Rebuild after each merged stage.
