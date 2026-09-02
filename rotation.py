#!/usr/bin/env python3
"""
Group-meeting presenter rotation + Slack reminders.

Everything is computed deterministically from schedule.json, so the whole
semester can be regenerated at any time and edits to the JSON are all that is
ever needed.

Rotation rule (from the lab):
  * People present in a fixed order, N per meeting (N = presenters_per_meeting).
  * If someone who is up is unavailable, the next person in line takes the slot
    and the skipped person presents at the next meeting they are available.
    Example: order A B C D E F.  Tue -> A, B.  Wed (D away) -> C, E.  Next -> D, F.
  * Special sessions (advisor workshop, AI training, ...) and no-meeting days
    pause the rotation: nobody is "used up" on those dates.

Commands
  python rotation.py validate                 check schedule.json for mistakes
  python rotation.py schedule                 print the semester schedule
  python rotation.py render-md                write SCHEDULE.md
  python rotation.py remind [--today D] [--dry-run] [--force] [--min-hour 10]
                                              post tomorrow's reminder to Slack
  python rotation.py post-schedule [--dry-run] post the remaining schedule to Slack
  python rotation.py test-post [--dry-run]    post a connectivity test message

Environment
  SLACK_WEBHOOK_URL     incoming-webhook URL for the channel (secret)
  GITHUB_SERVER_URL / GITHUB_REPOSITORY   set by GitHub Actions; used for links
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "schedule.json"
STATE_PATH = ROOT / "state.json"
SCHEDULE_MD_PATH = ROOT / "SCHEDULE.md"

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
class ConfigError(Exception):
    pass


@dataclass
class SpecialSession:
    title: str
    lead: Optional[str] = None


@dataclass
class Config:
    channel: str
    organizer: str
    timezone: str
    meeting_days: list[int]
    meeting_time: str
    presenters_per_meeting: int
    start_date: date
    end_date: date
    announce_no_meeting: bool
    rotation: list[str]
    slack_ids: dict[str, str]
    unavailable: dict[str, set[date]]
    special_sessions: dict[date, SpecialSession]
    no_meeting: dict[date, str]
    overrides: dict[date, list[str]]
    joined: dict[str, date]
    left: dict[str, date]
    warnings: list[str] = field(default_factory=list)

    # -- helpers ----------------------------------------------------------- #
    def is_meeting_day(self, d: date) -> bool:
        return d.weekday() in self.meeting_days

    def is_active(self, person: str, d: date) -> bool:
        j = self.joined.get(person)
        l = self.left.get(person)
        if j and d < j:
            return False
        if l and d >= l:
            return False
        return True

    def is_available(self, person: str, d: date) -> bool:
        return d not in self.unavailable.get(person, set())

    def mention(self, person: str) -> str:
        sid = (self.slack_ids.get(person) or "").strip()
        return f"<@{sid}>" if sid else f"*{person}*"


def parse_date(s: str, where: str) -> date:
    try:
        return date.fromisoformat(str(s).strip())
    except ValueError:
        raise ConfigError(f"{where}: '{s}' is not a valid date (use YYYY-MM-DD)")


def parse_date_or_range(s: str, where: str) -> set[date]:
    s = str(s).strip()
    for sep in ("/", "..", " to "):
        if sep in s:
            a, b = (x.strip() for x in s.split(sep, 1))
            start, end = parse_date(a, where), parse_date(b, where)
            if end < start:
                raise ConfigError(f"{where}: range '{s}' ends before it starts")
            if (end - start).days > 400:
                raise ConfigError(f"{where}: range '{s}' is longer than a year; probably a typo")
            return {start + timedelta(days=i) for i in range((end - start).days + 1)}
    return {parse_date(s, where)}


def _public(d: dict) -> dict:
    """Drop underscore-prefixed comment keys."""
    return {k: v for k, v in d.items() if not str(k).startswith("_")}


def load_config(path: Path = CONFIG_PATH) -> Config:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path.name} not found")
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path.name} is not valid JSON: {e}")
    raw = _public(raw)
    warnings: list[str] = []

    def req(key, typ):
        if key not in raw:
            raise ConfigError(f"missing required key '{key}'")
        if not isinstance(raw[key], typ):
            raise ConfigError(f"'{key}' has the wrong type")
        return raw[key]

    rotation = req("rotation", list)
    if not rotation or not all(isinstance(p, str) and p.strip() for p in rotation):
        raise ConfigError("'rotation' must be a non-empty list of names")
    rotation = [p.strip() for p in rotation]
    if len(set(rotation)) != len(rotation):
        raise ConfigError("'rotation' contains a duplicated name")
    known = set(rotation)

    days_raw = req("meeting_days", list)
    meeting_days = []
    for d in days_raw:
        key = str(d).strip().lower()[:3]
        if key not in WEEKDAYS:
            raise ConfigError(f"meeting_days: unknown weekday '{d}'")
        meeting_days.append(WEEKDAYS[key])
    if not meeting_days:
        raise ConfigError("'meeting_days' is empty")

    n = int(raw.get("presenters_per_meeting", 2))
    if n < 1:
        raise ConfigError("'presenters_per_meeting' must be at least 1")

    start = parse_date(req("start_date", str), "start_date")
    end = parse_date(req("end_date", str), "end_date")
    if end < start:
        raise ConfigError("end_date is before start_date")
    if start.weekday() not in meeting_days:
        raise ConfigError(
            f"start_date {start} is a {WEEKDAY_NAMES[start.weekday()]}, not a meeting day"
        )

    def check_names(section: dict, where: str):
        for name in section:
            if name not in known:
                raise ConfigError(
                    f"{where}: '{name}' is not in the rotation (names must match exactly; "
                    f"rotation is {', '.join(rotation)})"
                )

    unavailable: dict[str, set[date]] = {}
    for name, entries in _public(raw.get("unavailable", {}) or {}).items():
        check_names({name: None}, "unavailable")
        if isinstance(entries, str):
            entries = [entries]
        dates: set[date] = set()
        for e in entries:
            dates |= parse_date_or_range(e, f"unavailable.{name}")
        unavailable[name] = dates

    special: dict[date, SpecialSession] = {}
    for ds, spec in _public(raw.get("special_sessions", {}) or {}).items():
        d = parse_date(ds, "special_sessions")
        if isinstance(spec, str):
            spec = {"title": spec}
        title = str(spec.get("title", "")).strip() or "Special session"
        lead = spec.get("lead")
        special[d] = SpecialSession(title=title, lead=str(lead).strip() if lead else None)
        if d.weekday() not in meeting_days:
            raise ConfigError(
                f"special_sessions: {d} is a {WEEKDAY_NAMES[d.weekday()]}, not a meeting day"
            )

    no_meeting: dict[date, str] = {}
    for ds, reason in _public(raw.get("no_meeting", {}) or {}).items():
        d = parse_date(ds, "no_meeting")
        if d.weekday() not in meeting_days:
            raise ConfigError(f"no_meeting: {d} is a {WEEKDAY_NAMES[d.weekday()]}, not a meeting day")
        if d in special:
            raise ConfigError(f"{d} is listed in both special_sessions and no_meeting")
        no_meeting[d] = str(reason).strip() or "no meeting"

    overrides: dict[date, list[str]] = {}
    for ds, names in _public(raw.get("overrides", {}) or {}).items():
        d = parse_date(ds, "overrides")
        if d.weekday() not in meeting_days:
            raise ConfigError(f"overrides: {d} is a {WEEKDAY_NAMES[d.weekday()]}, not a meeting day")
        if d in special or d in no_meeting:
            raise ConfigError(f"overrides: {d} is also a special session / no-meeting day")
        if isinstance(names, str):
            names = [names]
        names = [str(x).strip() for x in names]
        check_names({x: None for x in names}, f"overrides.{ds}")
        if len(set(names)) != len(names):
            raise ConfigError(f"overrides.{ds}: duplicated name")
        if len(names) != n:
            warnings.append(f"overrides {d}: {len(names)} presenter(s) listed, usual number is {n}")
        overrides[d] = names

    joined = {k: parse_date(v, f"joined.{k}") for k, v in _public(raw.get("joined", {}) or {}).items()}
    left = {k: parse_date(v, f"left.{k}") for k, v in _public(raw.get("left", {}) or {}).items()}
    check_names(joined, "joined")
    check_names(left, "left")

    slack_ids = {k: str(v) for k, v in _public(raw.get("slack_ids", {}) or {}).items()}

    # Gentle warnings for things that are legal but probably unintended.
    for d in list(special) + list(no_meeting) + list(overrides):
        if not (start <= d <= end):
            warnings.append(f"{d} is outside start_date..end_date and will be ignored")
    for name, dates in unavailable.items():
        if dates and not any(start <= d <= end and d.weekday() in meeting_days for d in dates):
            warnings.append(f"unavailable.{name}: none of the dates fall on a meeting day this semester")

    cfg = Config(
        channel=str(raw.get("channel", "")),
        organizer=str(raw.get("organizer", "the organizer")),
        timezone=str(raw.get("timezone", "America/Indianapolis")),
        meeting_days=meeting_days,
        meeting_time=str(raw.get("meeting_time", "")),
        presenters_per_meeting=n,
        start_date=start,
        end_date=end,
        announce_no_meeting=bool(raw.get("announce_no_meeting", True)),
        rotation=rotation,
        slack_ids=slack_ids,
        unavailable=unavailable,
        special_sessions=special,
        no_meeting=no_meeting,
        overrides=overrides,
        joined=joined,
        left=left,
        warnings=warnings,
    )
    try:
        ZoneInfo(cfg.timezone)
    except Exception:
        raise ConfigError(f"unknown timezone '{cfg.timezone}'")
    return cfg


# --------------------------------------------------------------------------- #
# Rotation engine
# --------------------------------------------------------------------------- #
@dataclass
class Meeting:
    date: date
    kind: str                      # "regular" | "special" | "no_meeting"
    presenters: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # were up, but unavailable
    title: Optional[str] = None    # special-session title or no-meeting reason
    lead: Optional[str] = None
    note: Optional[str] = None

    @property
    def is_meeting(self) -> bool:
        return self.kind != "no_meeting"


def meeting_dates(cfg: Config):
    d = cfg.start_date
    while d <= cfg.end_date:
        if cfg.is_meeting_day(d):
            yield d
        d += timedelta(days=1)


def build_schedule(cfg: Config) -> list[Meeting]:
    queue: deque[str] = deque(cfg.rotation)
    n = cfg.presenters_per_meeting
    out: list[Meeting] = []

    for d in meeting_dates(cfg):
        if d in cfg.no_meeting:
            out.append(Meeting(d, "no_meeting", title=cfg.no_meeting[d]))
            continue
        if d in cfg.special_sessions:
            s = cfg.special_sessions[d]
            out.append(Meeting(d, "special", title=s.title, lead=s.lead))
            continue
        if d in cfg.overrides:
            chosen = list(cfg.overrides[d])
            for p in chosen:            # they've now presented: send to the back
                queue.remove(p)
                queue.append(p)
            out.append(Meeting(d, "regular", presenters=chosen, note="manual override"))
            continue

        chosen: list[str] = []
        skipped: list[str] = []
        for p in queue:
            if len(chosen) == n:
                break
            if not cfg.is_active(p, d):
                continue
            if cfg.is_available(p, d):
                chosen.append(p)
            else:
                skipped.append(p)
        for p in chosen:
            queue.remove(p)
            queue.append(p)
        note = None
        if len(chosen) < n:
            note = f"only {len(chosen)} presenter(s) available" if chosen else "nobody available"
        out.append(Meeting(d, "regular", presenters=chosen, skipped=skipped, note=note))
    return out


def meeting_on(schedule: list[Meeting], d: date) -> Optional[Meeting]:
    for m in schedule:
        if m.date == d:
            return m
    return None


def next_meeting_after(schedule: list[Meeting], d: date) -> Optional[Meeting]:
    for m in schedule:
        if m.date > d and m.is_meeting:
            return m
    return None


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def fmt_date(d: date) -> str:
    return f"{WEEKDAY_NAMES[d.weekday()]} {d:%b} {d.day}"


def join_names(names: list[str], fmt=lambda s: s) -> str:
    names = [fmt(x) for x in names]
    if not names:
        return "—"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " & " + names[-1]


def repo_links() -> dict[str, str]:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY")
    branch = os.environ.get("GITHUB_REF_NAME") or "main"
    if not repo:
        return {}
    base = f"{server}/{repo}"
    return {
        "schedule": f"{base}/blob/{branch}/SCHEDULE.md",
        "edit": f"{base}/edit/{branch}/schedule.json",
    }


def describe_line(m: Meeting, cfg: Config, mention: bool) -> str:
    """One-line description used in schedule lists."""
    fmt = cfg.mention if mention else (lambda s: s)
    if m.kind == "no_meeting":
        return f"no meeting ({m.title})"
    if m.kind == "special":
        lead = f" — {m.lead}" if m.lead else ""
        return f":sparkles: {m.title}{lead}" if mention else f"Special session: {m.title}{lead}"
    return join_names(m.presenters, fmt)


def render_reminder(m: Meeting, nxt: Optional[Meeting], cfg: Config) -> str:
    when = f"{fmt_date(m.date)}, {cfg.meeting_time}".rstrip(", ")
    links = repo_links()
    lines: list[str] = []

    if m.kind == "no_meeting":
        lines.append(f":no_entry: *No group meeting tomorrow ({fmt_date(m.date)}) — {m.title}.*")
        if nxt:
            lines.append(f"Next meeting: {fmt_date(nxt.date)} → {describe_line(nxt, cfg, mention=False)}")
    elif m.kind == "special":
        lines.append(f":sparkles: *Group meeting tomorrow — {when}*")
        lead = f" — led by {cfg.mention(m.lead) if m.lead in cfg.rotation else m.lead}" if m.lead else ""
        lines.append(f"Special session: *{m.title}*{lead}")
        if nxt:
            lines.append(
                f"No rotation talks tomorrow. Rotation resumes {fmt_date(nxt.date)} → "
                f"{describe_line(nxt, cfg, mention=False)}"
            )
    else:
        lines.append(f":calendar: *Group meeting tomorrow — {when}*")
        if m.presenters:
            lines.append(f"Presenting: {join_names(m.presenters, cfg.mention)}")
        else:
            lines.append("Presenting: _nobody is available — please sort it out in the thread_")
        if m.skipped:
            if len(m.skipped) == 1:
                lines.append(
                    f"_{m.skipped[0]} is out tomorrow, so the next person in line takes the slot; "
                    f"{m.skipped[0]} presents at the next meeting._"
                )
            else:
                lines.append(
                    f"_{join_names(m.skipped)} are out tomorrow, so the next people in line take the slots; "
                    f"they present at the next meeting they're available._"
                )
        if m.note == "manual override":
            lines.append("_(order set manually for this date)_")
        if nxt:
            lines.append(f"Up next: {fmt_date(nxt.date)} → {describe_line(nxt, cfg, mention=False)}")
        if nxt is None:
            lines.append("_This is the last group meeting of the semester._")

    if m.kind == "no_meeting":
        if links:
            lines.append(f"_<{links['schedule']}|Full schedule>_")
    else:
        footer = f"Can't present? Tell {cfg.organizer}"
        if links:
            footer = (
                f"Can't present? <{links['edit']}|Edit schedule.json> (or tell {cfg.organizer}) — "
                f"the rotation adjusts automatically · <{links['schedule']}|Full schedule>"
            )
        lines.append(f"_{footer}_")
    return "\n".join(lines)


def render_schedule_post(schedule: list[Meeting], cfg: Config, from_date: date) -> str:
    links = repo_links()
    days = " & ".join(WEEKDAY_NAMES[d] for d in sorted(cfg.meeting_days))
    lines = [f":calendar: *Group meeting schedule* ({days}, {cfg.meeting_time})"]
    for m in schedule:
        if m.date < from_date:
            continue
        lines.append(f"• {fmt_date(m.date)} — {describe_line(m, cfg, mention=False)}")
    if links:
        lines.append(f"_Full schedule & how to mark yourself out: <{links['schedule']}|SCHEDULE.md>_")
    return "\n".join(lines)


def render_markdown(schedule: list[Meeting], cfg: Config, today: Optional[date] = None) -> str:
    days = " & ".join(WEEKDAY_NAMES[d] for d in sorted(cfg.meeting_days))
    out = [
        "# Group meeting schedule",
        "",
        f"**{days}, {cfg.meeting_time}** · {cfg.channel} · "
        f"{cfg.presenters_per_meeting} presenters per meeting · "
        f"{fmt_date(cfg.start_date)} – {fmt_date(cfg.end_date)}, {cfg.end_date.year}",
        "",
        "> This file is generated automatically from `schedule.json` — edit that file, not this one.",
        "> A reminder is posted to Slack at 10:00 AM the day before each meeting.",
        "",
        f"Rotation order: {' → '.join(cfg.rotation)}",
        "",
        "| Date | Presenters | Notes |",
        "|---|---|---|",
    ]
    for m in schedule:
        notes = []
        if m.kind == "no_meeting":
            who = "_no meeting_"
            notes.append(m.title or "")
        elif m.kind == "special":
            who = f"✨ {m.title}"
            if m.lead:
                notes.append(f"led by {m.lead}")
            notes.append("rotation paused")
        else:
            who = join_names(m.presenters)
            if m.skipped:
                notes.append(f"{join_names(m.skipped)} out → presents next")
            if m.note:
                notes.append(m.note)
        marker = " ← next" if today and m.date >= today and not any(
            x.date >= today for x in schedule if x.date < m.date) else ""
        out.append(f"| {fmt_date(m.date)}{marker} | {who} | {'; '.join(n for n in notes if n)} |")

    counts = Counter(p for m in schedule for p in m.presenters)
    out += ["", "## Talks per person", ""]
    for p in cfg.rotation:
        out.append(f"- {p}: {counts.get(p, 0)}")
    out += [
        "",
        "## How the rotation works",
        "",
        f"People present in the order above, {cfg.presenters_per_meeting} per meeting. "
        "If someone who is up is unavailable, the next person in line takes the slot and the "
        "skipped person presents at the next meeting they can attend. Special sessions and "
        "no-meeting days pause the rotation — nobody loses their turn.",
        "",
        "To mark yourself out, add a special session, or cancel a meeting, edit `schedule.json` "
        "(the `_help` block at the top explains each field). Commit, and this file updates itself.",
        "",
    ]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Slack + state
# --------------------------------------------------------------------------- #
def post_to_slack(text: str, webhook_url: Optional[str] = None) -> None:
    url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        raise SystemExit(
            "SLACK_WEBHOOK_URL is not set. Add it as a repository secret "
            "(Settings → Secrets and variables → Actions)."
        )
    payload = json.dumps({"text": text, "unfurl_links": False, "unfurl_media": False}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Slack rejected the message: HTTP {e.code} {e.read().decode('utf-8', 'replace')}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Could not reach Slack: {e.reason}")
    if body.strip() != "ok":
        raise SystemExit(f"Unexpected reply from Slack: {body}")


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def now_local(cfg: Config, today_override: Optional[str]) -> datetime:
    tz = ZoneInfo(cfg.timezone)
    if today_override:
        d = parse_date(today_override, "--today")
        return datetime(d.year, d.month, d.day, 10, 0, tzinfo=tz)
    return datetime.now(tz)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_validate(args) -> int:
    cfg = load_config()
    schedule = build_schedule(cfg)
    for w in cfg.warnings:
        print(f"warning: {w}")
    n_meet = sum(1 for m in schedule if m.is_meeting)
    print(f"schedule.json OK — {len(schedule)} dates, {n_meet} meetings, "
          f"{len(cfg.rotation)} people, {fmt_date(cfg.start_date)} → {fmt_date(cfg.end_date)}")
    return 0


def cmd_schedule(args) -> int:
    cfg = load_config()
    for m in build_schedule(cfg):
        extra = ""
        if m.skipped:
            extra = f"   ({join_names(m.skipped)} out → presents next)"
        elif m.note:
            extra = f"   ({m.note})"
        print(f"{m.date}  {fmt_date(m.date):<11} {describe_line(m, cfg, mention=False)}{extra}")
    return 0


def cmd_render_md(args) -> int:
    cfg = load_config()
    today = now_local(cfg, args.today).date()
    SCHEDULE_MD_PATH.write_text(render_markdown(build_schedule(cfg), cfg, today), encoding="utf-8")
    print(f"wrote {SCHEDULE_MD_PATH.name}")
    return 0


def cmd_remind(args) -> int:
    cfg = load_config()
    now = now_local(cfg, args.today)
    today = now.date()
    tomorrow = today + timedelta(days=1)

    if not args.force and now.hour < args.min_hour:
        print(f"Local time is {now:%H:%M} ({cfg.timezone}); reminders go out after "
              f"{args.min_hour:02d}:00. Nothing to do.")
        return 0

    schedule = build_schedule(cfg)
    m = meeting_on(schedule, tomorrow)
    if m is None:
        print(f"No group meeting on {fmt_date(tomorrow)} ({tomorrow}). Nothing to do.")
        return 0
    if m.kind == "no_meeting" and not cfg.announce_no_meeting:
        print(f"{tomorrow} is a no-meeting day and announce_no_meeting is false. Nothing to do.")
        return 0

    state = load_state()
    if not args.force and state.get("last_reminder_for") == tomorrow.isoformat():
        print(f"Reminder for {tomorrow} was already posted at {state.get('posted_at_utc')}. Nothing to do.")
        return 0

    text = render_reminder(m, next_meeting_after(schedule, tomorrow), cfg)
    if args.dry_run:
        print("--- would post to Slack ---")
        print(text)
        return 0

    post_to_slack(text)
    save_state({
        "last_reminder_for": tomorrow.isoformat(),
        "posted_at_utc": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    })
    print(f"Posted reminder for {fmt_date(tomorrow)}.")
    return 0


def cmd_post_schedule(args) -> int:
    cfg = load_config()
    today = now_local(cfg, args.today).date()
    text = render_schedule_post(build_schedule(cfg), cfg, today)
    if args.dry_run:
        print(text)
        return 0
    post_to_slack(text)
    print("Posted schedule.")
    return 0


def cmd_test_post(args) -> int:
    cfg = load_config()
    today = now_local(cfg, None).date()
    text = (":white_check_mark: *Group Meeting Bot is connected.* Reminders will post here at "
            f"10:00 AM the day before each meeting ({' & '.join(WEEKDAY_NAMES[d] for d in sorted(cfg.meeting_days))}, "
            f"{cfg.meeting_time}). Next up: ")
    schedule = build_schedule(cfg)
    nxt = next((m for m in schedule if m.is_meeting and m.date >= today), None)
    text += f"{fmt_date(nxt.date)} → {describe_line(nxt, cfg, mention=False)}" if nxt else "— (no meetings left this semester)"
    if args.dry_run:
        print(text)
        return 0
    post_to_slack(text)
    print("Posted test message.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate").set_defaults(fn=cmd_validate)
    sub.add_parser("schedule").set_defaults(fn=cmd_schedule)

    p = sub.add_parser("render-md")
    p.add_argument("--today")
    p.set_defaults(fn=cmd_render_md)

    p = sub.add_parser("remind")
    p.add_argument("--today", help="pretend today is this date (YYYY-MM-DD)")
    p.add_argument("--dry-run", action="store_true", help="print instead of posting")
    p.add_argument("--force", action="store_true", help="ignore the time-of-day and already-posted checks")
    p.add_argument("--min-hour", type=int, default=10, help="earliest local hour to post (default 10)")
    p.set_defaults(fn=cmd_remind)

    p = sub.add_parser("post-schedule")
    p.add_argument("--today")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_post_schedule)

    p = sub.add_parser("test-post")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_test_post)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as e:
        print(f"schedule.json problem: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
