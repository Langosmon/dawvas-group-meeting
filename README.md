# DAWVAS group-meeting rotation bot

Posts a reminder to **#dawvas_shared** at **10:00 AM the day before every group meeting**
(Tuesdays and Wednesdays, 11:00–12:00) saying who presents, handles people who are away,
and pauses the rotation for special sessions or cancelled meetings.

Everything is driven by one file, **`schedule.json`**. `SCHEDULE.md` (the full semester at a
glance) is regenerated automatically every time that file changes.

What a reminder looks like:

> :calendar: **Group meeting tomorrow — Wed Sep 9, 11:00-12:00**
> Presenting: **Kristen** & **Danyang**
> Up next: Tue Sep 15 → Jose & Phillip
> _Can't present? Edit schedule.json (or tell Jose) — the rotation adjusts automatically · Full schedule_

If someone is out, the message says so ("_Danyang is out tomorrow, so the next person in line
takes the slot; Danyang presents at the next meeting._"). Special sessions get a :sparkles:
message with the title and who leads; no-meeting days get a short :no_entry: notice.

---

## One-time setup (about 15 minutes)

You need: permission to add an app to the Slack workspace, and a GitHub account.

### 1. Create a Slack incoming webhook for #dawvas_shared

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**.
2. Name it `Group Meeting Bot`, pick the lab's workspace, **Create App**.
3. In the left menu click **Incoming Webhooks** → turn the switch **On**.
4. Scroll down → **Add New Webhook to Workspace** → choose **#dawvas_shared** → **Allow**.
5. Copy the **Webhook URL** (starts with `https://hooks.slack.com/services/…`). Treat it like a
   password — anyone with it can post to the channel.
6. Optional but nice: **Basic Information → Display Information** → set the app name/icon that
   will appear on the messages.

If the workspace requires admin approval for new apps, Slack will show a "Request to install"
button instead of "Allow" — send the request; the app only needs the incoming-webhook permission.

### 2. Put these files in a GitHub repository

1. On GitHub: **New repository** → name it `dawvas-group-meeting` → **Private** is fine →
   leave "Add a README" unchecked → **Create repository**.
2. Upload the contents of this folder. Two ways:

   **With git (recommended)** — in a terminal, inside this folder:
   ```bash
   git init -b main
   git add .
   git commit -m "Group meeting rotation bot"
   git remote add origin https://github.com/<your-username>/dawvas-group-meeting.git
   git push -u origin main
   ```

   **Without git** — on the empty repo page click *uploading an existing file*, drag in
   `rotation.py`, `schedule.json`, `README.md`, `SCHEDULE.md` and the `tests` folder, and
   commit. Then click **Add file → Create new file**, type the name
   `.github/workflows/reminder.yml` (the slashes create the folders), paste the contents of
   that file from this folder, and commit. (Browsers often hide the `.github` folder when you
   drag and drop, which is why it's created by hand.)

The default branch must be called `main` (GitHub's default).

### 3. Give GitHub the webhook URL

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

* Name: `SLACK_WEBHOOK_URL`
* Secret: the URL you copied in step 1

### 4. Test it

Repo → **Actions** tab → **Group meeting reminder** (left) → **Run workflow** (right):

* Action `test-post` → posts ":white_check_mark: Group Meeting Bot is connected…" to the channel.
* Action `reminder-dry-run` with *today* = `2026-09-07` → shows in the run log exactly what will be
  posted the day before the first meeting, without posting it.
* Action `post-schedule` → posts the whole semester schedule to the channel (good to do once at
  the start of the semester).

That's it. From now on the reminder goes out automatically Monday and Tuesday at ~10:07 AM.
The first one will be **Monday Sep 7** for Kailey & Nathan.

---

## Everyday use — editing `schedule.json`

Open `schedule.json` on GitHub, click the pencil (Edit), change it, **Commit changes**.
Within a minute GitHub checks the file for typos (you get an email if something is wrong) and
regenerates `SCHEDULE.md`. The next reminder uses the new schedule automatically. Anyone with
write access to the repo can do this, so invite the whole lab
(**Settings → Collaborators**).

**Someone can't present** (conference, sick, travel) — add the date(s) under their name.
The next person in line takes the slot; the skipped person presents at the next meeting.

```json
"unavailable": {
  "Kristen": ["2026-09-23"],
  "Danyang": ["2026-10-05/2026-10-09", "2026-11-10"]
}
```
A range `START/END` includes both ends. Dates that aren't meeting days are simply ignored, so
it's fine to enter a whole trip.

**An advisor takes the slot** (workshop, AI training, guest talk) — add a special session.
The rotation pauses that day; nobody loses their turn.

```json
"special_sessions": {
  "2026-09-16": { "title": "AI session: using Agents on Purdue RCAC", "lead": "Jose" },
  "2026-10-21": { "title": "Proposal-writing workshop", "lead": "Dan Chavas" }
}
```

**No meeting at all** — add the date and a reason. A short "no meeting tomorrow" notice is posted
(set `"announce_no_meeting": false` to stay silent instead).

```json
"no_meeting": {
  "2026-10-13": "October break",
  "2026-11-25": "Thanksgiving"
}
```

**Force specific presenters on a date** (a swap, a make-up talk) — use an override. The people
listed count as having presented and go to the back of the line.

```json
"overrides": { "2026-11-03": ["Jose", "Kristen"] }
```

**Someone joins or leaves the lab** — add them to `rotation` (where you want them in the order)
and give the date:

```json
"rotation": ["Kailey", "Nathan", "Kristen", "Danyang", "Jose", "Phillip", "NewPerson"],
"joined": { "NewPerson": "2026-10-20" },
"left":   { "Phillip": "2026-11-15" }
```

**@-mention people in the reminder** — paste Slack member IDs into `slack_ids`
(in Slack: click the person's name → **⋯** → **Copy member ID**). Empty = plain name.

**Next semester** — change `start_date`, `end_date`, the `no_meeting` dates, and clear out
old `unavailable` entries. Set `start_date` to the first meeting day; the person listed first in
`rotation` presents that day.

Names in `unavailable`, `overrides`, `joined` and `left` must match `rotation` exactly
(capitalisation included) — the validator will tell you if one doesn't.

---

## How the rotation is decided

People are in a line in the order given by `rotation`. Each meeting takes the first two people
in line who are available that day and sends them to the back. Anyone who was skipped because
they were unavailable **stays at the front**, so they present at the very next meeting they can
attend. Example with the order A B C D E F:

| Meeting | Who | Line afterwards |
|---|---|---|
| Tue | A, B | C D E F A B |
| Wed (D away) | C, E | D F A B C E |
| next Tue | D, F | A B C E D F |

Special sessions and no-meeting days don't touch the line at all. Because everything is
recomputed from `schedule.json` each time, there is no hidden state to get out of sync — what
you see in `SCHEDULE.md` is exactly what the bot will announce.

## Manual actions (Actions tab → Run workflow)

| Action | What it does |
|---|---|
| `reminder-dry-run` | Print what would be posted for tomorrow (or for the *today* date you enter). Posts nothing. |
| `reminder` | Post tomorrow's reminder right now — e.g. after a last-minute change, to send a corrected one. |
| `post-schedule` | Post the remaining schedule for the semester to the channel. |
| `test-post` | Post a one-line "bot is connected" message. |

## Running it on your own computer

Python 3.9+ with no extra packages:

```bash
python rotation.py validate                       # check schedule.json
python rotation.py schedule                       # print the semester
python rotation.py remind --today 2026-09-07 --dry-run
python -m unittest discover -s tests -v           # run the tests
```

## If a reminder doesn't show up

* **Actions tab** shows every run and its log. A red run means something failed — the log says
  what (usually a typo in `schedule.json`, or a missing/expired webhook).
* GitHub's scheduler is sometimes late by a few minutes at busy times; the second run an hour
  later is a built-in safety net.
* GitHub **disables scheduled workflows in a repository with no commits for 60 days**
  (the bot's own commits count, so this only matters over long breaks). If that happens, the
  Actions tab shows a banner with an **Enable workflow** button.
* If the Slack app is removed or the webhook revoked, create a new webhook (step 1) and update
  the `SLACK_WEBHOOK_URL` secret.
* The two cron lines exist only to handle the change to/from daylight saving time — leave both.

## Files

| File | Purpose |
|---|---|
| `schedule.json` | The only file you edit: people, dates, exceptions. |
| `SCHEDULE.md` | Auto-generated full-semester schedule. Don't edit. |
| `rotation.py` | The engine: rotation rules, message text, Slack posting. |
| `.github/workflows/reminder.yml` | Runs the engine on schedule and on every edit. |
| `state.json` | Auto-generated; remembers the last reminder posted so it's never sent twice. |
| `tests/` | Unit tests for the rotation rules (run automatically on every edit). |
