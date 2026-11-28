# Threat model

What USMP protects, from whom, and where the limits are. It covers the reference implementation
(the `usmp` command and library) running on the user's own computer. Last reviewed: 2026-11-28.

## What USMP is

A local program. It reads data from a **source** service, saves a backup on disk, writes the data
into a **target** service and checks it arrived. There is no USMP server: data only travels
between the user's computer and the two services they chose.

## Assets

| Asset | Where it lives | Why it matters |
|---|---|---|
| Calendar data (titles, notes, places, attendees, meeting links and PINs) | source and target services; in memory during a run | Personal and often confidential |
| Backup bundle (`.usmp`) | a file the user chooses (default: current folder) | A complete copy of the calendar |
| Progress record (`<bundle>.progress.jsonl`) | next to the bundle | Item ids, target ids, target address and user name; loss details unless the backup is encrypted |
| Reports (JSON, Markdown, HTML) | `usmp-reports/` (or `--report-dir`) | Item ids, statuses, loss details (can name attendees or calendars), data warnings |
| Google OAuth tokens (refresh token, client id and secret) | OS keychain | Ongoing access to the Google calendar |
| CalDAV app passwords | OS keychain, or an environment variable the user names | Access to iCloud, Nextcloud, Fastmail… |
| OAuth client file (`client_secret.json`) | wherever the user saved it | Identifies the user's own OAuth app (not a strong secret for desktop apps) |
| Backup passphrase | typed at a prompt or an environment variable | Opens the encrypted backup |

## Who might attack

1. **Someone who gets the files later**: a lost laptop, a synced folder shared by mistake, a backup
   drive, cloud sync of the downloads folder.
2. **Other users or programs on the same computer** without the user's privileges.
3. **The network**: Wi-Fi, a hostile proxy.
4. **A malicious or broken input file**: a crafted `.usmp` or `.ics` file from elsewhere.
5. **A misbehaving service**: unexpected responses, rate limits, partial failures.
6. **A malicious connector plugin**: third-party Python packages installed by the user.

Out of scope: malware running as the user (it can read the keychain and memory), a compromised
operating system, and the providers themselves (Google, Apple…).

## Trust boundaries and mitigations

| Boundary | Risk | Mitigation |
|---|---|---|
| Source service → USMP | Changing or deleting the user's data in the old service | Sources are only used through `Reader`. Source HTTP clients refuse every write method (`PUT`, `DELETE`, `POST`…), tested by the conformance kit's read-only guard |
| USMP → target service | Duplicates, overwriting existing events | Conditional creates (`If-None-Match: *`), `X-USMP-ID` markers, retries only when they cannot write twice (spec §8.1); undo removes only what USMP created and nobody changed |
| Network | Reading or changing traffic | HTTPS with certificate checks (library defaults, no option to turn them off). CalDAV refuses `http://` except to this computer, unless the user adds `allow_http=yes` for a server on their own network |
| Disk: bundle | A full calendar copy readable by others | Bundle file created readable by the owner only (`0600`); optional passphrase encryption with age (`--encrypt`, ADR 0007); the plain ZIP never touches the disk when encrypting |
| Disk: reports, progress record | Personal details readable by others | Owner-only files (`0600`), report folder created `0700`; never credentials; with an encrypted backup the progress record leaves out loss details |
| Credentials | Leaking passwords or tokens | Kept in the OS keychain, or read from an environment variable the user names; never accepted on the command line, never in bundles, reports, progress records, printed commands or error messages. Configuration keys that look like secrets, and passwords inside URLs, are dropped from progress records. Google sign-in uses PKCE with the narrowest scope (read-only unless `--write`), and a refreshed token is stored back under the same access level |
| Input files (`.usmp`, `.ics`) | Zip bombs, path traversal, malformed data | Bundle reader checks entry names, counts, sizes and compression ratios before decompressing, verifies every hash, refuses symlinks and unexpected entries; parsing errors become plain messages |
| Printed commands | Shell injection when a user pastes the repeat command | Values are single-quoted with embedded quotes escaped |
| Connector plugins | Arbitrary code | Plugins are ordinary Python packages and run with the user's rights; only install connectors you trust. `usmp connectors list` shows what is installed |

## Residual risks

- **Reports are not encrypted.** They hold no titles, notes or times, but loss details can name
  attendees (for example a meeting room's address) and calendars. Keep the report folder private
  or delete reports you don't need. Tracked in [#63](https://github.com/starLORD03/usmp/issues/63).
- **The progress record** names the target address and user name (needed to resume), even next to
  an encrypted backup.
- **A plain backup is as sensitive as the calendar itself.** Without `--encrypt`, anyone who can
  read the file can read the calendar.
- **Passphrase strength** is only checked for length. age's scrypt slows guessing, but a short or
  common passphrase can still be guessed offline.
- **Memory**: an encrypted backup is decrypted in memory, and calendars are held in memory during a
  run. Large `.ics` files and server responses are not size-limited yet (a crafted file can use
  a lot of memory; [#64](https://github.com/starLORD03/usmp/issues/64)).
- **Error messages** from services are shortened but shown as received; they could include a URL
  or an event UID.
- **Third-party connectors** are trusted code ([#65](https://github.com/starLORD03/usmp/issues/65)).

## Reporting a vulnerability

See [SECURITY.md](https://github.com/starLORD03/usmp/blob/main/SECURITY.md).
