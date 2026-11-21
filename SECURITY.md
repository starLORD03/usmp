# Security Policy

USMP handles personal data and provider credentials, so security reports are taken seriously.

## Reporting a vulnerability

**Do not open a public issue.** Report it privately through GitHub:
[Report a vulnerability](https://github.com/starLORD03/usmp/security/advisories/new).

Please include the affected version or commit, steps to reproduce, and the impact. You will get an
acknowledgement within 7 days.

## Supported versions

Until 1.0, only the latest release receives security fixes.

## Scope

Of particular interest:

- Any path where a source connector could modify or delete source data.
- Credentials or tokens leaking into bundles, logs, reports or error messages.
- Bundle parsing issues (path traversal, zip bombs, schema bypass).
- Overly broad OAuth scopes.
