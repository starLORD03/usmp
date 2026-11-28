# Google Calendar

The `google` connector reads and writes Google Calendar through the official Calendar API.

```console
$ pipx install 'usmp-protocol[google]'
```

## 1. Create your own OAuth client (once, about 5 minutes)

USMP does not ship a shared Google app. You create a free client in your own Google Cloud
project, so your data and tokens never pass through anyone else.

1. Open [console.cloud.google.com](https://console.cloud.google.com) and create a project, e.g.
   `usmp`.
2. **APIs & Services → Library**: search for **Google Calendar API** and click **Enable**.
3. **APIs & Services → OAuth consent screen** (or **Google Auth Platform → Branding**):
   choose **External**, fill in an app name and your email, and save.
4. **Audience → Test users**: add the Google account(s) you will migrate.
5. **Clients → Create client**: application type **Desktop app**. Download the JSON file,
   e.g. `client_secret.json`. Keep it private.

!!! note "Testing mode is fine"
    While the app is in *Testing*, Google allows up to 100 test users and asks you to sign in
    again every 7 days. That is enough for personal migrations.

## 2. Sign in

```console
# Read-only: enough to migrate away from Google
$ usmp auth google --client-secrets client_secret.json

# Read and write: needed to migrate into Google
$ usmp auth google --client-secrets client_secret.json --write
```

A browser window opens for you to approve access. USMP asks only for
`calendar.readonly` (or `calendar` with `--write`). The token is stored in your OS keychain.

## 3. Migrate

```console
# Google → iCloud
$ usmp migrate --from google:account=you@gmail.com \
    --to caldav:url=https://caldav.icloud.com,username=you@icloud.com

# Google → .ics backup
$ usmp migrate --from google:account=you@gmail.com --to ics:path=google-backup/

# Only one calendar, into one target calendar
$ usmp migrate --from google:account=you@gmail.com,calendar=Work \
    --to caldav:url=…,username=…,calendar=Work
```

## What carries over

Titles, descriptions, locations, times and time zones, all-day events, recurring events with
exceptions, moved and deleted occurrences, attendees and their responses, rooms, reminders
(including "use default reminders"), availability, visibility, and calendar names, colours and
time zones.

**Meet links and dial-in numbers** become standard iCalendar `CONFERENCE` entries, and are also
added to the end of the event notes ("Join online: https://meet.google.com/…"), so they show in
Apple Calendar, Thunderbird and other apps that ignore `CONFERENCE`.

**Google-only details** — event colours, out-of-office, focus-time and birthday details,
"Created from Gmail" links, add-on data and guest permissions — are kept with each event (as
`X-USMP-PROVIDER-DATA` in iCalendar). Other apps ignore them, but migrating back into Google
restores them, including the same Meet link.

Reported as **declared losses** (shown before you migrate):

| Into Google | Why |
|---|---|
| Out-of-office, focus-time and birthday event types | Google only lets apps import ordinary events; the event itself is kept |
| Meeting links from other apps (Zoom, Teams, …) | Google can only attach meetings it created; the links are added to the notes |
| Group attendees | Google has no equivalent |
| Attachments | Google only stores Google Drive files |
| Chair and non-participant roles, delegated status | Google has no equivalent |
| Reminders after the start, sound alarms, more than 5 reminders | Google limits reminders |

Writing uses Google's `import` method: events keep their original iCalendar UID and organizer, and
**no invitations are sent** to attendees.

**Invitations from someone else** (or from your old address) are only accepted by Google if the
calendar they go into is the organizer or a guest. USMP adds that calendar as a guest, so in Google
Calendar you'll see it in the guest list. The original organizer and guests are kept unchanged, and
USMP leaves the added guest out when it reads or verifies the event. In the same way, when Google
adds a booked room's name to an event's location, USMP reads back the location as it was written.

## Manual end-to-end checklist

CI tests the connector against a simulated Google API. Before a release, also check with a real
test account (never your only copy):

- [ ] `usmp auth google` (read-only) and `--write` both succeed and print the account.
- [ ] Google → `.ics`: verified; recurring series with a moved and a deleted occurrence look right.
- [ ] `.ics` (1,000-event test calendar) → Google: 0 mismatch, 0 missing; no invitation emails sent.
- [ ] Google → iCloud: verified; events show at the right local time in both apps.
- [ ] Revoking access at [myaccount.google.com/permissions](https://myaccount.google.com/permissions)
      gives "sign-in expired or was revoked".
