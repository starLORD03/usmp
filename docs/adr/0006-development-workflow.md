# ADR 0006: Development workflow and CI/CD

- Status: Accepted
- Date: 2026-11-22
- Deciders: starLORD03

## Context

The project should be built in reviewable stages using git and CI/CD, with Claude Code and ECC assisting.

## Decision

- Trunk-based development: protected `main`, short-lived `feat/` `fix/` `docs/` `ci/` branches.
- Conventional Commits, enforced on PR titles; squash merges.
- Every change goes through a PR with required checks. PRs are squash-merged **only when all required checks pass**, by the maintainer or by an agent the maintainer has authorised. Agents never push to `main` directly.
- release-please handles versioning and the changelog; PyPI trusted publishing; docs deploy to GitHub Pages.
- The ECC workflow (PRD → plan → TDD → review → quality gate) is applied per stage. graphify and code-review-graph keep an architecture map and give change impact analysis.

## Consequences

- Slower than direct pushes, but every change is reviewed and reversible.
- Requires the `workflow` token scope for CI files and branch-protection setup.

## Amendment (2026-11-22)

The maintainer authorised Claude to manage and merge PRs. GitHub does not let an author approve
their own PR, and the maintainer and the agent share the `starLORD03` account. So branch protection
requires passing status checks (**CI OK** and **Conventional Commit title**), enforced for admins,
instead of an approving review. Changes to the spec or schemas that break compatibility, changes to
security or credentials handling, and anything that deletes data still need the maintainer's
explicit go-ahead before merging.
