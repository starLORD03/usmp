# Step by step: moving your calendar

Three common moves, start to finish. Each takes a few minutes. Your old calendar is only read,
never changed, and a backup of everything is saved before anything is written.

**Before you start:** install USMP with `pipx install 'usmp-protocol[google]'` (leave out
`[google]` if you don't use Google Calendar).

!!! tip "Try it on a test calendar first"
    Make a realistic 1,000-event test calendar and move that first:
    `python -m usmp.testing.synthetic test-calendar.ics`. Import it into a new, empty calendar
    on your old service, then follow the steps below with only that calendar
    (type its name when asked "Only this calendar").

## Google Calendar → iCloud

1. **Prepare Google sign-in (once).** Follow step 1 of the [Google guide](google.md)
   to download `client_secret.json`. It takes about five minutes.
2. **Create an iCloud app-specific password.** Go to
   [account.apple.com](https://account.apple.com) → Sign-In and Security → App-Specific
   Passwords, and create one called "USMP". Your normal Apple ID password won't work.
3. **Run the wizard:**

    ```console
    $ usmp migrate
    Where is your calendar now?
      1. Google Calendar
      ...
    Choose a number: 1
    Google account email: you@gmail.com
      Path to client_secret.json: ~/Downloads/client_secret.json
      Open the sign-in page in your browser now? [Y/n]:
    Only this calendar (type its name, or press Enter for all calendars):

    Where should it go?
    Choose a number: 2
    Apple ID email: you@icloud.com
      Password: ****-****-****-****
      Save it in your system keychain for next time? [Y/n]:

    Which events? Recurring events are always moved whole. ...
    Only events in this date range (press Enter to skip):
    ```

4. **Read the preview.** It lists how many calendars and events will move and anything that
   can't be carried over. Type `y` to go ahead.
5. **Check the result.** "✓ Migration verified" means every event was read back from iCloud and
   matched. The full report opens in a browser; the backup (`.usmp`) stays with you.

Each Google calendar becomes a new iCloud calendar with the same name and colour. If a calendar
with that name already exists, the new one is called `Work (2)`.

**What you'll notice in Apple Calendar:** Meet links appear at the end of each event's notes
("Join online: …") as well as in the event itself. Google colours per event, out-of-office and
birthday details are kept with each event and come back if you ever move into Google again.

## Google Calendar → Nextcloud

1. **Prepare Google sign-in** as in step 1 above.
2. **Create a Nextcloud app password:** Nextcloud → Settings → Security → Devices & sessions →
   *Create new app password*.
3. **Run `usmp migrate`**, choose **Google Calendar**, then **Nextcloud**, and give your
   Nextcloud address (`https://cloud.example.com`), user name and the app password.
4. **Preview, confirm, check** as above.

## A calendar file (.ics) → any CalDAV server

Use this for exports from Outlook, Thunderbird, Proton, an old Google Takeout, or any app that
can save `.ics`.

1. **Export** your calendar as `.ics` (one file per calendar, or a folder of them).
2. **Run `usmp migrate`**, choose **Calendar file (.ics)** and give the file or folder, then
   choose your target: iCloud, Nextcloud, Fastmail or **Other CalDAV server** (you'll need its
   address, for example `https://dav.example.com/`).
3. **Preview, confirm, check** as above.

## Doing it again, or without questions

At the end, the wizard prints the same migration as a single command:

```console
$ usmp migrate --from 'google:account=you@gmail.com' \
    --to 'caldav:url=https://caldav.icloud.com,username=you@icloud.com' --domain calendar
```

Add `--dry-run` to preview only, or `--yes` to skip the confirmation (for scripts).

## Moving only part of your calendar

Answer the wizard's date question, or add these to the command:

| Add | Moves |
|---|---|
| `--since 2025-01-01` | events that end on or after 1 January 2025 |
| `--until 2025-12-31` | events that start on or before 31 December 2025 (the whole day counts) |
| `--calendar Work` | only the calendar called Work (repeat for more: `--calendar Work --calendar Family`) |

```console
$ usmp migrate --from 'google:account=you@gmail.com' \
    --to 'caldav:url=https://caldav.icloud.com,username=you@icloud.com' \
    --since 2025-01-01 --calendar Work
```

A recurring event that has *any* occurrence in the range is moved whole, with every change made
to single occurrences, so no series is ever cut in two. The backup still holds everything, and
the report says how many items were left out. Those are not counted as missing.

## If it stops halfway

A lost connection, a closed laptop or Ctrl+C can stop a migration while it is writing. Nothing
written so far is lost, and USMP prints the command that finishes the job:

```console
$ usmp migrate --resume usmp-calendar-20261128-101500.usmp
```

It continues from the backup (your old calendar is not read again), skips everything already in
the new calendar, and verifies the lot. Running the whole migration again is also safe: events
that are already there are recognised and never written twice. A resumed migration keeps the
date range and calendars it started with.

## If something goes wrong

| You see | What to do |
|---|---|
| `sign-in failed … app-specific password` | Use an app password, not your normal password. |
| `Google sign-in expired or was revoked` | Run `usmp auth google --client-secrets client_secret.json` again. |
| `Error 403: access_denied` in the browser | Add your Google account as a test user of your OAuth client. |
| `event … already exists in the target calendar` | That calendar already has an event with the same id that USMP did not write. Nothing was overwritten. |
| "N item(s) were already in the target" | You ran this migration before. Those items were checked, not written again. |
| `no calendar named 'X'; available: …` | Use one of the names listed with `--calendar` (case doesn't matter). |
| `nothing matches the selection` | No event falls in that date range or those calendars; widen it. |
| `… kept failing; try again later` | The service stayed busy or unreachable after several tries over a few minutes. Wait a while, then run the `usmp migrate --resume …` command shown. |
| "Migration NOT fully verified" | Open the report: it lists each event that didn't match. The backup has the originals. |

Every error before writing ends with "Nothing was written to the target", so it's always safe to
fix the problem and run the wizard again.
