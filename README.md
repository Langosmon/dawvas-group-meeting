# DAWVAS group-meeting rotation bot

Keeps the group-meeting presenter rotation for **#dawvas_shared** running by itself:

* **Reminder** at 10:00 AM the day before every meeting (Tue & Wed, 11:00–12:00): who presents,
  who's next, and where to go if you can't make it.
* **Availability sheet** — a shared Google Sheet with one row per meeting date and one column per
  person. Type "out" in your cell and the bot re-plans; special sessions and cancelled meetings are
  typed into the same row. No GitHub account needed.
* **Change alerts** — within 30 minutes of an edit, the bot posts exactly which meetings changed
  ("Wed Sep 23: Kristen & Danyang → Danyang & Jose (Kristen out)").
* **Full schedule** always visible: `SCHEDULE.md` here, and the *Schedule* tab of the sheet.

```
:calendar: Group meeting tomorrow — Wed Sep 9, 11:00-12:00
Presenting: Kristen & Danyang
Up next: Tue Sep 15 → Jose & Phillip
Can't present? Type out under your name in the availability sheet (or tell Jose) — the rotation adjusts automatically · Full schedule
```

---

## Setup

### Already done
1. Slack app **Group Meeting Bot** with an incoming webhook for #dawvas_shared.
2. This repository, with the `SLACK_WEBHOOK_URL` secret. `test-post` confirmed the connection.

### Remaining (about 10 minutes)

**3. Create the availability sheet**

1. Go to <https://sheets.new> (a blank Google Sheet).
2. **File → Import → Upload** → pick `DAWVAS-group-meeting-availability.xlsx` (Claude sent it; or
   regenerate it with `python tools/make_sheet_template.py`).
   Import location: **Replace spreadsheet**. Then rename the file (top-left) to something like
   *DAWVAS group meeting availability*.
3. Check the tabs: **Availability** (the grid), **Schedule** (will fill in once the repo is public,
   step 5), **How to** (instructions for the lab).
4. **Share** (top right) → *General access* → **Anyone with the link** → **Editor** → Copy link.
   That's what lets new students edit without any account setup. (If you prefer, share with the
   lab's e-mail addresses as Editors instead — the bot only needs *Anyone with the link → Viewer*
   or better to read it.)

**4. Tell the bot which sheet**

The sheet's URL looks like `https://docs.google.com/spreadsheets/d/`**`1AbC…xyz`**`/edit#gid=0`.
The long part between `/d/` and `/edit` is the sheet ID.
Repo → **Settings → Secrets and variables → Actions → New repository secret**:
Name `GOOGLE_SHEET_ID`, value = that ID. (It's a secret because anyone holding it can open the sheet.)

**5. Make the repository public** (recommended)

Repo → **Settings → General** → *Danger Zone* → **Change visibility → Public**. This is what lets
the sheet's *Schedule* tab pull `schedule.csv`, lets people open `SCHEDULE.md` without logging in,
and makes the every-30-minutes sync free (private repos have a monthly Actions budget). The
webhook URL and the sheet ID stay in secrets, so nothing sensitive is exposed — only first names
and meeting dates. If you'd rather stay private, everything still works except the *Schedule* tab.

**6. Push the updated bot**

Unzip the new `dawvas-group-meeting.zip` over your local clone (it replaces `rotation.py`, the
workflows, `README.md`, `schedule.json` and the tests, and adds `tools/`), then:

```bash
git add -A
git commit -m "Availability sheet sync, swap-based rotation, change alerts"
git push
```

**7. Test it**

Actions → **Sync availability sheet** → Run workflow → mode `dry-run`. The log should say
`sheet read OK (28 date rows). 0 upcoming meeting(s) would change.` Then type `out` somewhere in the
sheet, run it again with mode `sync`, and watch the change alert arrive in #dawvas_shared (clear
the cell afterwards and sync again to undo). From then on it runs by itself every 30 minutes.

**8. Pin this in #dawvas_shared** (edit the link):

> 📅 **Group meeting rotation** — Tue & Wed 11–12, two presenters per meeting, in this order:
> Kailey → Nathan → Kristen → Danyang → Jose → Phillip. The bot posts a reminder the day before.
> **Can't present on a date?** Open the availability sheet ‹link›, find the row for that date and
> type *out* in your column. The next person swaps with you, nobody else moves, and the bot posts
> the change here within 30 min. Same sheet for special sessions (advisor workshop, AI training) and
> cancelled meetings — see its *How to* tab.

---

## Everyday use

### For everyone — the sheet
Open the sheet, find the date row, and:

| You want to… | Do this in that row |
|---|---|
| say you can't present | type anything in **your** column — `out`, `AMS conference`, `x`. Blank = available. |
| give the slot to a special session | fill **Special session (title)** and **Led by** |
| cancel the meeting | fill **No meeting (reason)** |
| force specific presenters (a swap you agreed, a make-up talk) | type their names in **Override presenters**, comma-separated |
| undo | clear the cell |

The grey columns (*Presenters*, *Notes*) update themselves within about an hour; the Slack change
alert arrives within 30 minutes. Please don't reorder or delete rows or rename headers.

### For the organizer — `schedule.json`
Edit on GitHub only for: the rotation order, semester start/end, meeting days/time, people
joining or leaving (`joined` / `left` with a date), and Slack member IDs for @-mentions. The four
sections `unavailable`, `special_sessions`, `no_meeting`, `overrides` are owned by the sheet and
get overwritten every sync — change those in the sheet.

**New semester:** update `start_date` / `end_date` (and the rotation if it changed), commit, then
regenerate the sheet: `python tools/make_sheet_template.py` and import it over the old one
(File → Import → Replace spreadsheet). Break dates go straight into the sheet's *No meeting* column.

---

## How the rotation is decided

People present in the order listed, two per meeting. Special sessions and no-meeting days are
skipped over — nobody loses their turn.

If someone marked **out** is scheduled, they **swap dates** with the next scheduled person who can
make it, and nobody else moves. So one absence changes exactly two meetings:

| Meeting | Planned | After D marks out on Wed |
|---|---|---|
| Tue | A, B | A, B |
| Wed | C, D | C, **E** |
| next Tue | E, F | **D**, F |
| next Wed | A, B | A, B (unchanged) |

When several swap partners are possible, the bot prefers one that doesn't give anyone two talks in
the same week. If nobody can trade (end of semester), the next free person in the order steps in.
Overrides pin a date's presenters; the people they displace inherit the override-people's next
turns, so everyone still presents equally often. Everything is recomputed from `schedule.json` each
time, so what `SCHEDULE.md` shows is exactly what the bot will announce.

## Manual actions (Actions tab → Run workflow)

**Group meeting reminder**

| Action | What it does |
|---|---|
| `reminder-dry-run` | Print what would be posted for tomorrow (or for the *today* date you enter). |
| `reminder` | Post tomorrow's reminder now — e.g. a corrected one after a last-minute change. |
| `post-schedule` | Post the remaining semester schedule to the channel. |
| `test-post` | Post a one-line "bot is connected" message. |

**Sync availability sheet**

| Mode | What it does |
|---|---|
| `dry-run` | Read the sheet, show what would change, touch nothing. |
| `sync` | Read the sheet, update `schedule.json`, post changes to Slack (this is what runs every 30 min). |

## Running it on your own computer

Python 3.9+, no extra packages (except `openpyxl` for the sheet template):

```bash
python rotation.py validate
python rotation.py schedule
python rotation.py remind --today 2026-09-07 --dry-run
python rotation.py sync-sheet --dry-run --csv-file some-export.csv     # test the sheet parser offline
GOOGLE_SHEET_ID=… python rotation.py sync-sheet --dry-run              # read the real sheet
python -m unittest discover -s tests -v
```

## If something looks wrong

* **Actions tab** shows every run and its log. Red = failed; the log says why (a webhook problem,
  a sheet that can't be read, a typo in `schedule.json`).
* **"got a web page instead of CSV"** in the sync log → the sheet isn't shared with *Anyone with the
  link*. Fix sharing; no code change needed.
* **Sheet edit didn't show up in Slack** → nothing about the outcome changed (e.g. you marked out
  on a date you weren't presenting), or the next sync hasn't run yet (every 30 min at :13 and :43).
  The sync log says `0 upcoming meeting(s) changed` in the first case.
* **Refusing to sync ("only N date rows")** → the sheet lost most of its rows. Restore them
  (File → Version history) — the bot deliberately won't wipe the schedule.
* **Schedule tab shows an error** → the repo must be public; or paste the formula from the *How to*
  tab into `Schedule!A1`.
* GitHub's scheduler can be a few minutes late; the reminder job has a second run an hour later as
  a safety net, and never posts twice.
* GitHub disables scheduled workflows after **60 days without any commit**; the bot's own commits
  normally prevent that, but after a long break look for the *Enable workflow* banner in Actions.

## Files

| File | Purpose |
|---|---|
| `schedule.json` | Rotation, semester dates, meeting days, joins/leaves, Slack IDs — plus the four sheet-synced sections. |
| `SCHEDULE.md` / `schedule.csv` | Auto-generated schedule (Markdown for humans, CSV for the sheet's *Schedule* tab). |
| `rotation.py` | The engine: rotation rules, sheet sync, messages, Slack posting. |
| `.github/workflows/reminder.yml` | Day-before reminders; validates/tests on every edit. |
| `.github/workflows/sync-sheet.yml` | Reads the sheet every 30 minutes and posts change alerts. |
| `tools/make_sheet_template.py` | Builds the `.xlsx` to import into Google Sheets (needs `openpyxl`). |
| `state.json` | Auto-generated; remembers the last reminder posted so it's never sent twice. |
| `tests/` | 32 unit tests (rotation rules, sheet parsing, alerts); run on every push. |
