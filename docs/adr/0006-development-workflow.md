# ADR 0006: Development workflow and CI/CD

- Status: Proposed
- Date: 2026-11-22
- Deciders: starLORD03

## Context

The project should be built in reviewable stages using git and CI/CD, with Claude Code and ECC assisting.

## Decision

- Trunk-based development: protected `main`, short-lived `feat/` `fix/` `docs/` `ci/` branches.
- Conventional Commits, enforced on PR titles; squash merges.
- Every change goes through a PR with required checks. **The maintainer reviews and merges.** Agents never push to `main`.
- release-please handles versioning and the changelog; PyPI trusted publishing; docs deploy to GitHub Pages.
- The ECC workflow (PRD → plan → TDD → review → quality gate) is applied per stage. graphify and code-review-graph keep an architecture map and give change impact analysis.

## Consequences

- Slower than direct pushes, but every change is reviewed and reversible.
- Requires the `workflow` token scope for CI files and branch-protection setup.
