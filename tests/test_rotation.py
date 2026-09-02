"""Tests for the rotation engine. Run with:  python -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rotation as r  # noqa: E402


def make_config(**overrides) -> r.Config:
    """Small A..F lab that meets Tue/Wed like the real one."""
    base = {
        "channel": "#test",
        "organizer": "A",
        "timezone": "America/Indianapolis",
        "meeting_days": ["Tue", "Wed"],
        "meeting_time": "11:00-12:00",
        "presenters_per_meeting": 2,
        "start_date": "2026-09-08",
        "end_date": "2026-09-30",
        "rotation": ["A", "B", "C", "D", "E", "F"],
        "unavailable": {},
        "special_sessions": {},
        "no_meeting": {},
        "overrides": {},
    }
    base.update(overrides)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(base, fh)
        path = Path(fh.name)
    try:
        return r.load_config(path)
    finally:
        path.unlink()


def presenters(cfg):
    return [(m.date.isoformat(), m.kind, m.presenters) for m in r.build_schedule(cfg)]


class RotationRules(unittest.TestCase):
    def test_plain_rotation_two_per_meeting(self):
        cfg = make_config(end_date="2026-09-16")
        self.assertEqual(
            [p for _, _, p in presenters(cfg)],
            [["A", "B"], ["C", "D"], ["E", "F"], ["A", "B"]],
        )

    def test_unavailable_person_is_skipped_and_presents_next(self):
        # Tue: A,B.  Wed: D is away -> C,E.  Next Tue: D,F.  Then A,B again.
        cfg = make_config(end_date="2026-09-16", unavailable={"D": ["2026-09-09"]})
        sched = r.build_schedule(cfg)
        self.assertEqual(sched[0].presenters, ["A", "B"])
        self.assertEqual(sched[1].presenters, ["C", "E"])
        self.assertEqual(sched[1].skipped, ["D"])
        self.assertEqual(sched[2].presenters, ["D", "F"])
        self.assertEqual(sched[3].presenters, ["A", "B"])

    def test_two_people_unavailable_same_day(self):
        cfg = make_config(end_date="2026-09-16", unavailable={"C": ["2026-09-09"], "D": ["2026-09-09"]})
        sched = r.build_schedule(cfg)
        self.assertEqual(sched[1].presenters, ["E", "F"])
        self.assertEqual(sched[1].skipped, ["C", "D"])
        self.assertEqual(sched[2].presenters, ["C", "D"])

    def test_date_range_unavailability(self):
        cfg = make_config(end_date="2026-09-23", unavailable={"C": ["2026-09-07/2026-09-18"]})
        sched = r.build_schedule(cfg)
        # C misses Sep 9, 15 and 16 (all inside the range) and stays at the front of
        # the line the whole time, then presents at the first meeting back (Sep 22).
        self.assertEqual(sched[1].presenters, ["D", "E"])
        self.assertEqual(sched[2].presenters, ["F", "A"])
        self.assertEqual(sched[3].presenters, ["B", "D"])
        self.assertEqual(sched[3].skipped, ["C"])
        self.assertEqual(sched[4].date, date(2026, 9, 22))
        self.assertEqual(sched[4].presenters, ["C", "E"])

    def test_special_session_pauses_rotation(self):
        cfg = make_config(
            end_date="2026-09-23",
            special_sessions={"2026-09-16": {"title": "AI session: Agents on RCAC", "lead": "E"}},
        )
        sched = r.build_schedule(cfg)
        self.assertEqual(sched[0].presenters, ["A", "B"])   # Tue Sep 8
        self.assertEqual(sched[1].presenters, ["C", "D"])   # Wed Sep 9
        self.assertEqual(sched[2].presenters, ["E", "F"])   # Tue Sep 15
        self.assertEqual(sched[3].kind, "special")          # Wed Sep 16
        self.assertEqual(sched[3].presenters, [])
        self.assertEqual(sched[4].presenters, ["A", "B"])   # Tue Sep 22 — nobody lost a turn

    def test_no_meeting_pauses_rotation(self):
        cfg = make_config(end_date="2026-09-16", no_meeting={"2026-09-09": "Fall break"})
        sched = r.build_schedule(cfg)
        self.assertEqual(sched[1].kind, "no_meeting")
        self.assertEqual(sched[1].title, "Fall break")
        self.assertEqual(sched[2].presenters, ["C", "D"])

    def test_override_pins_presenters_and_sends_them_to_the_back(self):
        cfg = make_config(end_date="2026-09-23", overrides={"2026-09-09": ["E", "A"]})
        sched = r.build_schedule(cfg)
        self.assertEqual(sched[1].presenters, ["E", "A"])
        self.assertEqual(sched[1].note, "manual override")
        # Queue after Sep 8 (A,B presented): C D E F A B ; override moves E and A back -> C D F B E A
        self.assertEqual(sched[2].presenters, ["C", "D"])
        self.assertEqual(sched[3].presenters, ["F", "B"])
        self.assertEqual(sched[4].presenters, ["E", "A"])

    def test_left_and_joined(self):
        cfg = make_config(
            end_date="2026-09-23",
            rotation=["A", "B", "C", "D", "E", "F", "G"],
            joined={"G": "2026-09-16"},
            left={"B": "2026-09-15"},
        )
        sched = r.build_schedule(cfg)
        self.assertEqual(sched[0].presenters, ["A", "B"])   # Sep 8: B still here, G not yet
        self.assertEqual(sched[1].presenters, ["C", "D"])   # Sep 9
        self.assertEqual(sched[2].presenters, ["E", "F"])   # Sep 15: G not yet active
        self.assertEqual(sched[3].presenters, ["G", "A"])   # Sep 16: G active, B gone
        self.assertNotIn("B", [p for m in sched[2:] for p in m.presenters])

    def test_nobody_available(self):
        cfg = make_config(
            end_date="2026-09-09",
            unavailable={p: ["2026-09-09"] for p in "ABCDEF"},
        )
        sched = r.build_schedule(cfg)
        self.assertEqual(sched[1].presenters, [])
        self.assertEqual(sched[1].note, "nobody available")
        # Everyone was skipped, so everyone is still in line: the message must not crash.
        txt = r.render_reminder(sched[1], None, cfg)
        self.assertIn("nobody is available", txt)

    def test_only_one_available(self):
        cfg = make_config(end_date="2026-09-09",
                          unavailable={p: ["2026-09-09"] for p in "BCDEF"})
        sched = r.build_schedule(cfg)
        self.assertEqual(sched[1].presenters, ["A"])
        self.assertEqual(sched[1].note, "only 1 presenter(s) available")


class ConfigValidation(unittest.TestCase):
    def test_unknown_name_rejected(self):
        with self.assertRaises(r.ConfigError):
            make_config(unavailable={"Zed": ["2026-09-09"]})

    def test_bad_date_rejected(self):
        with self.assertRaises(r.ConfigError):
            make_config(unavailable={"A": ["09/09/2026"]})

    def test_special_session_must_be_on_meeting_day(self):
        with self.assertRaises(r.ConfigError):
            make_config(special_sessions={"2026-09-10": {"title": "Thursday?"}})

    def test_start_date_must_be_meeting_day(self):
        with self.assertRaises(r.ConfigError):
            make_config(start_date="2026-09-07")

    def test_duplicate_rotation_name_rejected(self):
        with self.assertRaises(r.ConfigError):
            make_config(rotation=["A", "B", "A"])

    def test_underscore_keys_are_ignored(self):
        cfg = make_config(_help=["hi"], unavailable={"_example": ["2026-09-09"], "A": []})
        self.assertEqual(cfg.unavailable, {"A": set()})

    def test_real_schedule_json_is_valid(self):
        cfg = r.load_config(r.CONFIG_PATH)
        sched = r.build_schedule(cfg)
        self.assertEqual(sched[0].date, date(2026, 9, 8))
        self.assertEqual(sched[0].presenters, ["Kailey", "Nathan"])
        self.assertEqual(sched[1].presenters, ["Kristen", "Danyang"])
        self.assertEqual(r.meeting_on(sched, date(2026, 9, 16)).kind, "special")
        self.assertEqual(r.meeting_on(sched, date(2026, 10, 13)).kind, "no_meeting")
        self.assertEqual(r.meeting_on(sched, date(2026, 11, 25)).kind, "no_meeting")
        self.assertEqual(sched[-1].date, date(2026, 12, 9))


class Rendering(unittest.TestCase):
    def setUp(self):
        os.environ.pop("GITHUB_REPOSITORY", None)

    def test_regular_reminder_mentions_presenters_and_next(self):
        cfg = make_config(end_date="2026-09-16", unavailable={"D": ["2026-09-09"]})
        sched = r.build_schedule(cfg)
        txt = r.render_reminder(sched[1], r.next_meeting_after(sched, sched[1].date), cfg)
        self.assertIn("Wed Sep 9", txt)
        self.assertIn("*C* & *E*", txt)
        self.assertIn("D is out tomorrow", txt)
        self.assertIn("Up next: Tue Sep 15 → D & F", txt)

    def test_slack_ids_become_mentions(self):
        cfg = make_config(end_date="2026-09-08", slack_ids={"A": "U111", "B": ""})
        sched = r.build_schedule(cfg)
        txt = r.render_reminder(sched[0], None, cfg)
        self.assertIn("<@U111> & *B*", txt)

    def test_special_and_no_meeting_reminders(self):
        cfg = make_config(
            end_date="2026-09-16",
            special_sessions={"2026-09-09": {"title": "Workshop", "lead": "Dan Chavas"}},
            no_meeting={"2026-09-15": "Break"},
        )
        sched = r.build_schedule(cfg)
        special = r.render_reminder(sched[1], r.next_meeting_after(sched, sched[1].date), cfg)
        self.assertIn("Special session: *Workshop* — led by Dan Chavas", special)
        self.assertIn("Rotation resumes Wed Sep 16 → C & D", special)
        off = r.render_reminder(sched[2], r.next_meeting_after(sched, sched[2].date), cfg)
        self.assertIn("No group meeting tomorrow (Tue Sep 15) — Break", off)
        self.assertIn("Next meeting: Wed Sep 16 → C & D", off)

    def test_markdown_renders_all_dates(self):
        cfg = make_config()
        md = r.render_markdown(r.build_schedule(cfg), cfg, today=date(2026, 9, 10))
        self.assertIn("| Tue Sep 8 |", md)
        self.assertIn("Tue Sep 15 ← next", md)
        self.assertIn("## Talks per person", md)


class RemindCommand(unittest.TestCase):
    """End-to-end checks of `remind` using --today and --dry-run (no network)."""

    def run_remind(self, *argv):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = r.main(["remind", "--dry-run", *argv])
        return code, buf.getvalue()

    def test_day_before_meeting_posts(self):
        code, out = self.run_remind("--today", "2026-09-07")
        self.assertEqual(code, 0)
        self.assertIn("Kailey", out)

    def test_no_meeting_tomorrow_is_quiet(self):
        code, out = self.run_remind("--today", "2026-09-09")   # tomorrow is Thursday
        self.assertEqual(code, 0)
        self.assertIn("No group meeting", out)
        self.assertNotIn("would post", out)

    def test_after_semester_is_quiet(self):
        code, out = self.run_remind("--today", "2026-12-15")
        self.assertEqual(code, 0)
        self.assertNotIn("would post", out)


if __name__ == "__main__":
    unittest.main()
