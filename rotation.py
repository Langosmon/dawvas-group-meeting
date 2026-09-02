#!/usr/bin/env python3
"""
Group-meeting presenter rotation + Slack reminders.

Everything is computed deterministically from schedule.json, so the whole
semester can be regenerated at any time and edits to the JSON are all that is
ever needed.

Rotation rule (from the lab):
  * People present in a fixed order, N per meeting (N = presenters_per_meeting).
  * If someone who is up is unavailable, they swap dates with the next scheduled
    person who can make it; nobody else moves.
    Example: order A B C D E F.  Tue -> A, B.  Wed (D away) -> C, E.  Next -> D, F.
    Then A, B again exactly as originally planned.
  * Special sessions (advisor workshop, AI training, ...) and no-meeting days
    pause the rotation: nobody is "used up" on those dates.

Where the information comes from:
  * schedule.json holds the rotation, the semester dates and the meeting days.
  * A shared Google Sheet ("Availability" tab) is where people mark themselves
    out and where special sessions / cancelled meetings are entered.
    `sync-sheet` copies it into the unavailable / special_sessions / no_meeting /
    overrides sections of schedule.json and posts a "schedule updated" alert
    to Slack when the outcome changed.  No Google credentials are needed: the
    sheet is read through its CSV export, which works for link-shared sheets.

Commands
  python rotation.py validate                 check schedule.json for mistakes
  python rotation.py schedule                 print the semester schedule
  python rotation.py render                   write SCHEDULE.md and schedule.csv
  python rotation.py remind [--today D] [--dry-run] [--force] [--min-hour 10]
                                              post tomorrow's reminder to Slack
  python rotation.py sync-sheet [--dry-run] [--csv-file F] [--today D]
                                              pull the Google Sheet into schedule.json
  python rotation.py sheet-template           print the Availability tab as CSV
  python rotation.py post-schedule [--dry-run] post the remaining schedule to Slack
  python rotation.py test-post [--dry-run]    post a connectivity test message

Environment
  SLACK_WEBHOOK_URL     incoming-webhook URL for the channel (secret)
  GOOGLE_SHEET_ID       the long id in the sheet's URL (secret; the sheet is link-shared)
  GOOGLE_SHEET_TAB      tab name to read (default "Availability")
  SHEET_CSV_URL         optional: full CSV URL, overrides the two above
  GITHUB_SERVER_URL / GITHUB_REPOSITORY   set by GitHub Actions; used for links
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "schedule.json"
STATE_PATH = ROOT / "state.json"
SCHEDULE_MD_PATH = ROOT / "SCHEDULE.md"
SCHEDULE_CSV_PATH = ROOT / "schedule.csv"

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


def load_config(path: Optional[Path] = None) -> Config:
    path = path or CONFIG_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path.name} not found")
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path.name} is not valid JSON: {e}")
    return config_from_dict(raw)


def config_from_dict(raw: dict) -> Config:
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
class Swap:
    out: str                        # the person who couldn't make it
    stand_in: Optional[str]         # who took their slot (None = nobody could)
    new_date: Optional[date]        # when the absent person presents instead


@dataclass
class Meeting:
    date: date
    kind: str                      # "regular" | "special" | "no_meeting"
    presenters: list[str] = field(default_factory=list)
    swaps: list[Swap] = field(default_factory=list)     # absences resolved on this date
    title: Optional[str] = None    # special-session title or no-meeting reason
    lead: Optional[str] = None
    note: Optional[str] = None

    @property
    def is_meeting(self) -> bool:
        return self.kind != "no_meeting"

    @property
    def skipped(self) -> list[str]:
        """People who were scheduled for this date but were unavailable."""
        return [s.out for s in self.swaps]


def meeting_dates(cfg: Config):
    d = cfg.start_date
    while d <= cfg.end_date:
        if cfg.is_meeting_day(d):
            yield d
        d += timedelta(days=1)


def build_schedule(cfg: Config) -> list[Meeting]:
    """
    1. Lay out the baseline: walk the rotation cyclically, N people per regular
       meeting. Special sessions and no-meeting days consume nobody's turn.
    2. Resolve absences, in date order, as SWAPS: an unavailable person trades
       dates with the next scheduled person who can make it. Nobody else moves,
       so one absence changes exactly two meetings.  (A B C D E F: Tue A,B;
       Wed with D away -> C,E; next Tue -> D,F; then A,B as originally planned.)
    3. Overrides pin a date's presenters; the people they displace take the
       override-person's next slot, so everyone still presents equally often.
    """
    n = cfg.presenters_per_meeting
    order = cfg.rotation
    dates = list(meeting_dates(cfg))
    fixed: dict[date, Meeting] = {}
    slots: dict[date, list[Optional[str]]] = {}

    # --- 1. baseline ------------------------------------------------------- #
    ptr = 0
    for d in dates:
        if d in cfg.no_meeting:
            fixed[d] = Meeting(d, "no_meeting", title=cfg.no_meeting[d])
            continue
        if d in cfg.special_sessions:
            s = cfg.special_sessions[d]
            fixed[d] = Meeting(d, "special", title=s.title, lead=s.lead)
            continue
        today: list[Optional[str]] = []
        tries = 0
        while len(today) < n and tries < len(order):
            p = order[ptr % len(order)]
            tries += 1
            if not cfg.is_active(p, d):
                ptr += 1            # skip people who haven't joined / have left
                continue
            if p in today:
                break               # fewer active people than slots
            today.append(p)
            ptr += 1
        while len(today) < n:
            today.append(None)
        slots[d] = today

    regular = [d for d in dates if d in slots]
    swaps: dict[date, list[Swap]] = {d: [] for d in regular}
    notes: dict[date, Optional[str]] = {d: None for d in regular}

    def future_slot(person: str, start: int, avoid_date: Optional[date] = None):
        """First (index, position) after `start` where `person` holds a slot."""
        for j in range(start + 1, len(regular)):
            if regular[j] == avoid_date:
                continue
            for k, q in enumerate(slots[regular[j]]):
                if q == person:
                    return j, k
        return None

    # --- 2./3. resolve overrides and absences in date order ----------------- #
    for i, d in enumerate(regular):
        cur = slots[d]

        if d in cfg.overrides:
            wanted = list(cfg.overrides[d])
            missing = [w for w in wanted if w not in cur]
            for idx, p in enumerate(cur):
                if p in wanted:
                    continue
                if not missing:
                    cur[idx] = None             # override lists fewer people than slots
                    continue
                w = missing.pop(0)
                # Hand the displaced person the override-person's next turn.
                if p is not None:
                    hit = future_slot(w, i)
                    while hit is not None and p in slots[regular[hit[0]]]:
                        hit = future_slot(w, hit[0])
                    if hit is not None:
                        slots[regular[hit[0]]][hit[1]] = p
                cur[idx] = w
            for w in missing:                   # override lists more people than slots
                cur.append(w)
            notes[d] = "manual override"
            continue

        def same_week(a: date, b: date) -> bool:
            return a.isocalendar()[:2] == b.isocalendar()[:2]

        def has_slot_in_week(person: str, when: date, except_date: date) -> bool:
            return any(person in slots[x] for x in regular if x != except_date and same_week(x, when))

        for idx, p in enumerate(cur):
            if p is None or cfg.is_available(p, d):
                continue
            # Candidate swaps, earliest first. Prefer one that gives nobody two talks in
            # the same week; fall back to the earliest workable one.
            best, fallback = None, None
            for j in range(i + 1, len(regular)):
                d2 = regular[j]
                if p in slots[d2]:
                    continue                    # p already presents on d2
                for k, q in enumerate(slots[d2]):
                    if q is None or q in cur:
                        continue
                    if not (cfg.is_available(q, d) and cfg.is_active(q, d)):
                        continue
                    if not (cfg.is_available(p, d2) and cfg.is_active(p, d2)):
                        continue
                    if fallback is None:
                        fallback = (j, k, q)
                    if not has_slot_in_week(q, d, d2) and not has_slot_in_week(p, d2, d):
                        best = (j, k, q)
                        break
                if best:
                    break
            partner = best or fallback
            if partner:
                j, k, q = partner
                cur[idx] = q
                slots[regular[j]][k] = p
                swaps[d].append(Swap(p, q, regular[j]))
                continue
            # No future slot to trade (e.g. end of semester): the next available person
            # in rotation order simply steps in.
            start = order.index(p) if p in order else 0
            stand_in = None
            for step in range(1, len(order)):
                q = order[(start + step) % len(order)]
                if q not in cur and cfg.is_available(q, d) and cfg.is_active(q, d):
                    stand_in = q
                    break
            cur[idx] = stand_in
            swaps[d].append(Swap(p, stand_in, None))

    # --- assemble ------------------------------------------------------------ #
    rank = {p: i for i, p in enumerate(order)}
    out: list[Meeting] = []
    for d in dates:
        if d in fixed:
            out.append(fixed[d])
            continue
        people = sorted((p for p in slots[d] if p), key=lambda p: rank.get(p, 999))
        note = notes[d]
        if len(people) < n and note is None:
            note = f"only {len(people)} presenter(s) available" if people else "nobody available"
        out.append(Meeting(d, "regular", presenters=people, swaps=swaps[d], note=note))
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


def swap_note(sw: Swap, tomorrow: bool = False) -> str:
    when = "tomorrow" if tomorrow else "that day"
    if sw.stand_in is None:
        return f"{sw.out} is out {when} and nobody could swap in"
    if sw.new_date is not None:
        return f"{sw.out} is out {when} → {sw.stand_in} steps in; {sw.out} presents {fmt_date(sw.new_date)} instead"
    return f"{sw.out} is out {when} → {sw.stand_in} steps in"


def render_reminder(m: Meeting, nxt: Optional[Meeting], cfg: Config) -> str:
    when = f"{fmt_date(m.date)}, {cfg.meeting_time}".rstrip(", ")
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
        for sw in m.swaps:
            lines.append(f"_{swap_note(sw, tomorrow=True)}._")
        if m.note == "manual override":
            lines.append("_(order set manually for this date)_")
        if nxt:
            lines.append(f"Up next: {fmt_date(nxt.date)} → {describe_line(nxt, cfg, mention=False)}")
        if nxt is None:
            lines.append("_This is the last group meeting of the semester._")

    lines.append(f"_{footer_text(cfg, for_no_meeting=(m.kind == 'no_meeting'))}_")
    return "\n".join(lines)


def footer_text(cfg: Config, for_no_meeting: bool = False) -> str:
    """Where to go to change things: the Google Sheet if configured, else the JSON."""
    links = repo_links()
    sheet = sheet_edit_url()
    full = f" · <{links['schedule']}|Full schedule>" if links else ""
    if for_no_meeting:
        if sheet:
            return f"<{sheet}|Availability sheet>{full}".lstrip(" ·")
        return f"<{links['schedule']}|Full schedule>" if links else ""
    if sheet:
        return (f"Can't present? Type *out* under your name in the <{sheet}|availability sheet> "
                f"(or tell {cfg.organizer}) — the rotation adjusts automatically{full}")
    if links:
        return (f"Can't present? <{links['edit']}|Edit schedule.json> (or tell {cfg.organizer}) — "
                f"the rotation adjusts automatically{full}")
    return f"Can't present? Tell {cfg.organizer}"


def render_csv(schedule: list[Meeting], cfg: Config) -> str:
    """Compact CSV of the schedule, imported live by the Google Sheet's 'Schedule' tab."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Date", "Day", "Presenters", "Notes"])
    for m in schedule:
        notes = []
        if m.kind == "no_meeting":
            who = "no meeting"
            notes.append(m.title or "")
        elif m.kind == "special":
            who = f"Special session: {m.title}"
            if m.lead:
                notes.append(f"led by {m.lead}")
        else:
            who = join_names(m.presenters)
            for sw in m.swaps:
                notes.append(swap_note(sw))
            if m.note:
                notes.append(m.note)
        w.writerow([m.date.isoformat(), WEEKDAY_NAMES[m.date.weekday()], who, "; ".join(n for n in notes if n)])
    return buf.getvalue()


def render_schedule_post(schedule: list[Meeting], cfg: Config, from_date: date) -> str:
    days = " & ".join(WEEKDAY_NAMES[d] for d in sorted(cfg.meeting_days))
    lines = [f":calendar: *Group meeting schedule* ({days}, {cfg.meeting_time})"]
    for m in schedule:
        if m.date < from_date:
            continue
        lines.append(f"• {fmt_date(m.date)} — {describe_line(m, cfg, mention=False)}")
    foot = footer_text(cfg)
    if foot:
        lines.append(f"_{foot}_")
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
        "> This file is generated automatically — don't edit it. To mark yourself out or add a "
        "special session, use the shared **Availability** Google Sheet (link pinned in Slack); "
        "the bot re-reads it every 30 minutes.",
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
            for sw in m.swaps:
                notes.append(swap_note(sw))
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
        "If someone who is up is unavailable, they swap dates with the next scheduled person who "
        "can make it — nobody else moves. Special sessions and no-meeting days pause the rotation; "
        "nobody loses their turn.",
        "",
        "To mark yourself out, type *out* in your column of the Availability sheet for that date. "
        "To add a special session or cancel a meeting, fill the *Special session* or *No meeting* "
        "column of that row. Within 30 minutes the bot updates this page and posts what changed in Slack.",
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
# Google Sheet sync
# --------------------------------------------------------------------------- #
class SheetError(Exception):
    pass


# Anything else typed in a person's cell means "not available". Blank = available.
AVAILABLE_WORDS = {"", "ok", "okay", "yes", "y", "available", "in", "here", "present", "✓", "✔", "fine"}

# Header names we recognise (compared case-insensitively, after trimming).
HEADER_DATE = {"date"}
HEADER_SPECIAL = {"special session", "special session (title)", "special", "special session title"}
HEADER_LEAD = {"led by", "lead", "leader", "special session lead"}
HEADER_NO_MEETING = {"no meeting", "no meeting (reason)", "no meeting reason", "cancelled", "canceled"}
HEADER_OVERRIDE = {"override presenters", "override", "force presenters", "override (names)"}
MIN_ROWS_FOR_SYNC = 3   # refuse to sync from a sheet that looks accidentally emptied


def sheet_id() -> str:
    return os.environ.get("GOOGLE_SHEET_ID", "").strip()


def sheet_edit_url() -> Optional[str]:
    explicit = os.environ.get("SHEET_URL", "").strip()
    if explicit:
        return explicit
    sid = sheet_id()
    return f"https://docs.google.com/spreadsheets/d/{sid}/edit" if sid else None


def sheet_csv_urls() -> list[str]:
    urls = []
    explicit = os.environ.get("SHEET_CSV_URL", "").strip()
    if explicit:
        urls.append(explicit)
    sid = sheet_id()
    if sid:
        tab = os.environ.get("GOOGLE_SHEET_TAB", "").strip() or "Availability"
        urls.append(f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&headers=1"
                    f"&sheet={urllib.parse.quote(tab)}")
        urls.append(f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid=0")
    return urls


def fetch_sheet_csv() -> str:
    urls = sheet_csv_urls()
    if not urls:
        raise SheetError("GOOGLE_SHEET_ID (or SHEET_CSV_URL) is not set — add it as a repository secret.")
    errors = []
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "group-meeting-bot/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8-sig", "replace")
        except urllib.error.HTTPError as e:
            errors.append(f"HTTP {e.code} from {url.split('?')[0]}")
            continue
        except urllib.error.URLError as e:
            errors.append(f"{e.reason} from {url.split('?')[0]}")
            continue
        if body.lstrip().startswith("<"):
            errors.append("got a web page instead of CSV — is the sheet shared as "
                          "'Anyone with the link' (Viewer or Editor)?")
            continue
        return body
    raise SheetError("Could not read the Google Sheet: " + "; ".join(errors))


def parse_sheet_date(s: str, cfg: Config) -> Optional[date]:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y",
                "%d %b %Y", "%d %B %Y", "%a %b %d %Y", "%a, %b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    # No year given ("Tue Sep 8", "Sep 8"): pick the year that lands in the semester.
    for fmt in ("%a %b %d", "%b %d", "%A %B %d", "%B %d", "%a, %b %d"):
        try:
            t = datetime.strptime(s, fmt)
        except ValueError:
            continue
        for y in sorted({cfg.start_date.year, cfg.end_date.year}):
            try:
                d = date(y, t.month, t.day)
            except ValueError:
                continue
            if cfg.start_date <= d <= cfg.end_date:
                return d
    return None


@dataclass
class SheetData:
    unavailable: dict[str, list[str]]
    special_sessions: dict[str, dict]
    no_meeting: dict[str, str]
    overrides: dict[str, list[str]]
    warnings: list[str]
    rows: int


def parse_sheet(text: str, cfg: Config) -> SheetData:
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        raise SheetError("the sheet is empty")
    header = [h.strip().casefold() for h in rows[0]]

    def find(names: set[str]) -> Optional[int]:
        for i, h in enumerate(header):
            if h in names:
                return i
        return None

    date_col = find(HEADER_DATE)
    if date_col is None:
        raise SheetError(f"no 'Date' column in the first row (found: {', '.join(rows[0])})")
    by_fold = {p.casefold(): p for p in cfg.rotation}
    people_cols = {by_fold[h]: i for i, h in enumerate(header) if h in by_fold}
    special_col, lead_col = find(HEADER_SPECIAL), find(HEADER_LEAD)
    no_meeting_col, override_col = find(HEADER_NO_MEETING), find(HEADER_OVERRIDE)

    warnings: list[str] = []
    missing = [p for p in cfg.rotation if p not in people_cols]
    if missing:
        warnings.append(f"the sheet has no column for {join_names(missing)} — they can't mark themselves out there")

    unavailable: dict[str, list[str]] = {}
    special: dict[str, dict] = {}
    no_meeting: dict[str, str] = {}
    overrides: dict[str, list[str]] = {}
    seen: set[date] = set()
    n_rows = 0

    for r in rows[1:]:
        def cell(i: Optional[int]) -> str:
            return r[i].strip() if i is not None and i < len(r) else ""

        raw_date = cell(date_col)
        d = parse_sheet_date(raw_date, cfg)
        if d is None:
            if raw_date:
                warnings.append(f"row with date '{raw_date}' not understood — ignored")
            continue
        n_rows += 1
        if d in seen:
            warnings.append(f"{fmt_date(d)} appears twice in the sheet — using both rows")
        seen.add(d)
        if not (cfg.start_date <= d <= cfg.end_date) or not cfg.is_meeting_day(d):
            warnings.append(f"{fmt_date(d)} ({d}) is not a meeting day this semester — ignored")
            continue
        iso = d.isoformat()

        for person, i in people_cols.items():
            if cell(i).casefold() not in AVAILABLE_WORDS:
                unavailable.setdefault(person, []).append(iso)

        nm, sp = cell(no_meeting_col), cell(special_col)
        if nm:
            no_meeting[iso] = nm
        if sp:
            if nm:
                warnings.append(f"{fmt_date(d)}: both 'No meeting' and 'Special session' are filled — treating it as no meeting")
            else:
                entry = {"title": sp}
                if cell(lead_col):
                    entry["lead"] = cell(lead_col)
                special[iso] = entry

        ov = cell(override_col)
        if ov:
            names = [x.strip() for x in re.split(r"[,&/;+]+|\band\b", ov) if x.strip()]
            resolved = []
            for nme in names:
                if nme.casefold() in by_fold and by_fold[nme.casefold()] not in resolved:
                    resolved.append(by_fold[nme.casefold()])
                else:
                    warnings.append(f"{fmt_date(d)}: override name '{nme}' isn't in the rotation — ignored")
            if resolved and not nm and not sp:
                overrides[iso] = resolved

    if n_rows < MIN_ROWS_FOR_SYNC:
        raise SheetError(f"only {n_rows} date row(s) found — refusing to sync in case the sheet was emptied by accident")

    return SheetData(
        unavailable={p: sorted(set(v)) for p, v in sorted(unavailable.items())},
        special_sessions=dict(sorted(special.items())),
        no_meeting=dict(sorted(no_meeting.items())),
        overrides=dict(sorted(overrides.items())),
        warnings=warnings,
        rows=n_rows,
    )


def sheet_template_rows(cfg: Config) -> list[list[str]]:
    """The Availability tab, pre-filled from the current schedule.json."""
    header = ["Date", "Day", "Presenters (auto)", "Notes (auto)", *cfg.rotation,
              "Special session (title)", "Led by", "No meeting (reason)", "Override presenters"]
    rows = [header]
    for d in meeting_dates(cfg):
        iso = d.isoformat()
        row = [iso, WEEKDAY_NAMES[d.weekday()], "", ""]
        for p in cfg.rotation:
            row.append("out" if d in cfg.unavailable.get(p, set()) else "")
        sp = cfg.special_sessions.get(d)
        row += [sp.title if sp else "", (sp.lead or "") if sp else "",
                cfg.no_meeting.get(d, ""), ", ".join(cfg.overrides.get(d, []))]
        rows.append(row)
    return rows


def apply_sheet_to_config(data: SheetData, path: Optional[Path] = None) -> bool:
    """Write the sheet-owned sections into schedule.json. Returns True if the file changed."""
    path = path or CONFIG_PATH
    text = path.read_text(encoding="utf-8")
    raw = json.loads(text)
    raw["unavailable"] = data.unavailable
    raw["special_sessions"] = data.special_sessions
    raw["no_meeting"] = data.no_meeting
    raw["overrides"] = data.overrides
    new_text = json.dumps(raw, indent=2, ensure_ascii=False) + "\n"
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def diff_schedules(old: list[Meeting], new: list[Meeting], from_date: date):
    """Meetings on/after from_date whose outcome changed: list of (date, old, new)."""
    by_old = {m.date: m for m in old}
    by_new = {m.date: m for m in new}
    changes = []
    for d in sorted(set(by_old) | set(by_new)):
        if d < from_date:
            continue
        a, b = by_old.get(d), by_new.get(d)
        key = lambda m: (m.kind, tuple(m.presenters), m.title, m.lead) if m else None  # noqa: E731
        if key(a) != key(b):
            changes.append((d, a, b))
    return changes


def render_change_alert(changes, cfg: Config, warnings: list[str], limit: int = 12) -> str:
    lines = [":pencil2: *Group meeting schedule updated*"]
    for d, a, b in changes[:limit]:
        was = describe_line(a, cfg, mention=False) if a else "—"
        now = describe_line(b, cfg, mention=False) if b else "—"
        why = ""
        if b and b.kind == "regular" and b.skipped:
            why = f" ({join_names(b.skipped)} out)"
        elif b and b.kind == "regular" and b.note == "manual override":
            why = " (set manually)"
        lines.append(f"• {fmt_date(d)}: {was} → *{now}*{why}")
    if len(changes) > limit:
        lines.append(f"…and {len(changes) - limit} more")
    for w in warnings[:5]:
        lines.append(f":warning: {w}")
    foot = footer_text(cfg)
    if foot:
        lines.append(f"_{foot}_")
    return "\n".join(lines)


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
        if m.swaps:
            extra = "   (" + "; ".join(swap_note(sw) for sw in m.swaps) + ")"
        elif m.note:
            extra = f"   ({m.note})"
        print(f"{m.date}  {fmt_date(m.date):<11} {describe_line(m, cfg, mention=False)}{extra}")
    return 0


def cmd_render(args) -> int:
    cfg = load_config()
    today = now_local(cfg, args.today).date()
    schedule = build_schedule(cfg)
    SCHEDULE_MD_PATH.write_text(render_markdown(schedule, cfg, today), encoding="utf-8")
    SCHEDULE_CSV_PATH.write_text(render_csv(schedule, cfg), encoding="utf-8")
    print(f"wrote {SCHEDULE_MD_PATH.name} and {SCHEDULE_CSV_PATH.name}")
    return 0


def cmd_sheet_template(args) -> int:
    cfg = load_config()
    w = csv.writer(sys.stdout, lineterminator="\n")
    for row in sheet_template_rows(cfg):
        w.writerow(row)
    return 0


def cmd_sync_sheet(args) -> int:
    cfg_before = load_config()
    today = now_local(cfg_before, args.today).date()
    schedule_before = build_schedule(cfg_before)

    try:
        text = Path(args.csv_file).read_text(encoding="utf-8-sig") if args.csv_file else fetch_sheet_csv()
        data = parse_sheet(text, cfg_before)
    except SheetError as e:
        print(f"sheet sync skipped: {e}", file=sys.stderr)
        return 3

    for w in data.warnings:
        print(f"warning: {w}")

    # Build the would-be config first so a bad sheet can never leave a broken schedule.json behind.
    merged = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    merged.update({"unavailable": data.unavailable, "special_sessions": data.special_sessions,
                   "no_meeting": data.no_meeting, "overrides": data.overrides})
    try:
        cfg_after = config_from_dict(merged)
    except ConfigError as e:
        print(f"sheet sync skipped: the sheet would make schedule.json invalid ({e})", file=sys.stderr)
        return 3
    changes = diff_schedules(schedule_before, build_schedule(cfg_after), today)

    if args.dry_run:
        print(f"sheet read OK ({data.rows} date rows). {len(changes)} upcoming meeting(s) would change.")
        if changes:
            print("--- would post to Slack ---")
            print(render_change_alert(changes, cfg_after, data.warnings))
        return 0

    if not apply_sheet_to_config(data):
        print(f"sheet read OK ({data.rows} date rows); schedule.json already up to date.")
        return 0
    print(f"schedule.json updated from the sheet ({data.rows} date rows); "
          f"{len(changes)} upcoming meeting(s) changed.")
    if changes:
        text = render_change_alert(changes, cfg_after, data.warnings)
        if args.no_post:
            print(text)
        else:
            post_to_slack(text)
            print("Posted change alert.")
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

    for name in ("render", "render-md"):          # render-md kept as an alias
        p = sub.add_parser(name)
        p.add_argument("--today")
        p.set_defaults(fn=cmd_render)

    sub.add_parser("sheet-template").set_defaults(fn=cmd_sheet_template)

    p = sub.add_parser("sync-sheet")
    p.add_argument("--today", help="pretend today is this date (YYYY-MM-DD)")
    p.add_argument("--dry-run", action="store_true", help="show what would change; touch nothing")
    p.add_argument("--no-post", action="store_true", help="update schedule.json but don't post to Slack")
    p.add_argument("--csv-file", help="read this CSV file instead of the Google Sheet (testing)")
    p.set_defaults(fn=cmd_sync_sheet)

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
