# CalDAV: iCloud, Nextcloud, Fastmail

The `caldav` connector reads and writes any CalDAV server. As a **source** it uses a client that
refuses every request that could change data, so your old calendar cannot be modified. As a
**target** it creates new calendars and never overwrites existing events.

## Store your password once

Passwords are never typed on the command line. Store yours in the OS keychain:

```console
$ usmp auth set caldav you@icloud.com@https://caldav.icloud.com
Password: ********
```

The account is `<username>@<url>`, exactly as in the connector options. Alternatively, put the
password in an environment variable and pass `password_env=VAR_NAME`.

!!! note "iCloud needs an app-specific password"
    Create one at [account.apple.com](https://account.apple.com) → Sign-In and Security →
    App-Specific Passwords. Your normal Apple ID password will be refused.

## Servers

| Service | `url` | `username` |
|---|---|---|
| iCloud | `https://caldav.icloud.com` | your Apple ID email |
| Nextcloud | `https://cloud.example.com/remote.php/dav` | your Nextcloud user |
| Fastmail | `https://caldav.fastmail.com/dav/` | your Fastmail email |
| Radicale | `http://localhost:5232/` | your Radicale user |

## Examples

```console
# Preview a move from an .ics export into iCloud
$ usmp migrate --from ics:path=google-export.ics \
    --to caldav:url=https://caldav.icloud.com,username=you@icloud.com --dry-run

# Put everything into one existing (or new) calendar called "Home"
$ usmp migrate --from ics:path=export/ \
    --to caldav:url=https://caldav.icloud.com,username=you@icloud.com,calendar=Home

# Back up a Nextcloud calendar to .ics files
$ usmp migrate --from caldav:url=https://cloud.example.com/remote.php/dav,username=me \
    --to ics:path=backup/
```

If a calendar with the same name already exists on the target, a new one is created with a
suffix (`Work (2)`) and the rename is shown in the report as a declared loss.

## Manual end-to-end checklist

CI tests the connector against a Radicale server on every change. Before a release, also run
this against real accounts (use test calendars, never your only copy):

- [ ] **iCloud → .ics:** `usmp migrate --from caldav:…icloud… --to ics:path=out/` is verified.
- [ ] **.ics → iCloud:** the 1,000-event test calendar (`python -m usmp.testing.synthetic test-calendar.ics`)
      migrates with 0 mismatch and 0 missing.
- [ ] Recurring events with a moved occurrence and an exception show correctly in Apple Calendar.
- [ ] All-day events stay on the right day in a zone different from yours.
- [ ] Running the same migration again writes nothing: the report says every item was already
      in the target, and no `Work (2)` calendar appears.
- [ ] Stopping a migration halfway (Ctrl+C) and running `usmp migrate --resume <backup>` finishes
      it with no duplicates.
- [ ] A wrong password gives "sign-in failed … app-specific password".
- [ ] **Nextcloud:** the same four migrations, plus colours and descriptions show in the web UI.
- [ ] The source account is unchanged afterwards: compare event counts before and after.
