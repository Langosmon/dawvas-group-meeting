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

    def test_unavailable_person_swaps_with_next(self):
        # Tue: A,B.  Wed: D is away -> C,E.  Next Tue: D,F.  Then A,B again — nothing else moves.
        cfg = make_config(end_date="2026-09-23", unavailable={"D": ["2026-09-09"]})
        sched = r.build_schedule(cfg)
        self.assertEqual(sched[0].presenters, ["A", "B"])
        self.assertEqual(sched[1].presenters, ["C", "E"])
        self.assertEqual(sched[1].skipped, ["D"])
        self.assertEqual(sched[1].swaps[0].stand_in, "E")
        self.assertEqual(sched[1].swaps[0].new_date, date(2026, 9, 15))
        self.assertEqual(sched[2].presenters, ["D", "F"])
        self.assertEqual(sched[3].presenters, ["A", "B"])
        self.assertEqual(sched[4].presenters, ["C", "D"])   # original pairs resume
        self.assertEqual(sched[5].presenters, ["E", "F"])

    def test_one_absence_changes_exactly_two_meetings(self):
        base = r.build_schedule(make_config(end_date="2026-12-09"))
        changed = r.build_schedule(make_config(end_date="2026-12-09", unavailable={"D": ["2026-09-09"]}))
        diffs = r.diff_schedules(base, changed, date(2026, 9, 1))
        self.assertEqual([d for d, _, _ in diffs], [date(2026, 9, 9), date(2026, 9, 15)])

    def test_two_people_unavailable_same_day(self):
        cfg = make_config(end_date="2026-09-16", unavailable={"C": ["2026-09-09"], "D": ["2026-09-09"]})
        sched = r.build_schedule(cfg)
        self.assertEqual(sched[1].presenters, ["E", "F"])
        self.assertEqual(sched[1].skipped, ["C", "D"])
        self.assertEqual(sched[2].presenters, ["C", "D"])

    def test_date_range_unavailability(self):
        cfg = make_config(end_date="2026-10-07", unavailable={"C": ["2026-09-07/2026-09-18"]})
        sched = r.build_schedule(cfg)
        # C can't do Sep 9. The nearest trades (Sep 15/16) are dates C can't make either,
        # and Sep 23 would give C two talks in one week (C already has Sep 22), so the
        # swap goes to E's Oct 6 slot. Everyone else is untouched.
        self.assertEqual(sched[1].presenters, ["D", "E"])
        self.assertEqual(sched[1].swaps[0].new_date, date(2026, 10, 6))
        self.assertEqual(sched[2].presenters, ["E", "F"])
        self.assertEqual(sched[3].presenters, ["A", "B"])
        self.assertEqual(sched[4].presenters, ["C", "D"])   # Sep 22, C's own slot
        self.assertEqual([m.presenters for m in sched if m.date == date(2026, 10, 6)], [["C", "F"]])

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

    def test_override_pins_presenters_and_displaced_take_their_slots(self):
        cfg = make_config(end_date="2026-09-23", overrides={"2026-09-09": ["E", "A"]})
        sched = r.build_schedule(cfg)
        self.assertEqual(sched[1].presenters, ["A", "E"])
        self.assertEqual(sched[1].note, "manual override")
        # C (displaced by E) takes E's Sep 15 slot; D (displaced by A) takes A's Sep 16 slot.
        self.assertEqual(sched[2].presenters, ["C", "F"])
        self.assertEqual(sched[3].presenters, ["B", "D"])
        self.assertEqual(sched[4].presenters, ["C", "D"])
        counts = {p: sum(p in m.presenters for m in sched) for p in "ABCDEF"}
        self.assertEqual(set(counts.values()), {2})

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
        self.assertEqual(sched[3].presenters, ["A", "G"])   # Sep 16: G active, B gone
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
        # No future slot to trade (semester ends), so the next free person just steps in.
        self.assertEqual(sched[1].presenters, ["A"])
        self.assertEqual(sched[1].note, "only 1 presenter(s) available")
        self.assertEqual((sched[1].swaps[0].out, sched[1].swaps[0].stand_in), ("C", "A"))


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
        self.assertIn("D is out tomorrow → E steps in; D presents Tue Sep 15 instead", txt)
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


class SheetSync(unittest.TestCase):
    HEADER = "Date,Day,Presenters (auto),A,B,C,D,E,F,Special session (title),Led by,No meeting (reason),Override presenters"

    def sheet(self, *rows):
        return "\n".join([self.HEADER, *rows]) + "\n"

    def test_parse_marks_people_out_and_reads_exceptions(self):
        cfg = make_config(end_date="2026-09-30")
        text = self.sheet(
            "2026-09-08,Tue,,,,,,,,,,,",
            "2026-09-09,Wed,,,,,AMS conference,ok,,,,,",          # D out (any text), E 'ok' = available
            "9/15/2026,Tue,,,,,,,x,,,,",                          # US date format, F out
            "Wed Sep 16,Wed,,,,,,,,Proposal workshop,Dan,,",      # no year, special session
            "2026-09-22,Tue,,,,,,,,,,Fall break,",
            "2026-09-23,Wed,,,,,,,,,,,\"E, A\"",                  # override
        )
        data = r.parse_sheet(text, cfg)
        self.assertEqual(data.unavailable, {"D": ["2026-09-09"], "F": ["2026-09-15"]})
        self.assertEqual(data.special_sessions, {"2026-09-16": {"title": "Proposal workshop", "lead": "Dan"}})
        self.assertEqual(data.no_meeting, {"2026-09-22": "Fall break"})
        self.assertEqual(data.overrides, {"2026-09-23": ["E", "A"]})
        self.assertEqual(data.warnings, [])

    def test_parse_ignores_junk_rows_and_warns(self):
        cfg = make_config()
        text = self.sheet(
            "2026-09-08,Tue,,,,,,,,,,,",
            "2026-09-09,Wed,,,,,,,,,,,",
            "2026-09-15,Tue,,,,,,,,,,,",
            "2026-09-10,Thu,,,out,,,,,,,,",           # not a meeting day
            "please leave blank if available,,,,,,,,,,,,",
        )
        data = r.parse_sheet(text, cfg)
        self.assertEqual(data.unavailable, {})
        self.assertEqual(len(data.warnings), 2)

    def test_parse_refuses_nearly_empty_sheet(self):
        cfg = make_config()
        with self.assertRaises(r.SheetError):
            r.parse_sheet(self.sheet("2026-09-08,Tue,,,,,,,,,,,"), cfg)
        with self.assertRaises(r.SheetError):
            r.parse_sheet("Foo,Bar\n1,2\n3,4\n5,6\n", cfg)      # no Date column

    def test_parse_warns_about_missing_person_column(self):
        cfg = make_config(rotation=["A", "B", "C", "D", "E", "F", "G"])
        data = r.parse_sheet(self.sheet("2026-09-08,,,,,,,,,,,,", "2026-09-09,,,,,,,,,,,,", "2026-09-15,,,,,,,,,,,,"), cfg)
        self.assertTrue(any("G" in w for w in data.warnings))

    def test_sheet_template_round_trips(self):
        cfg = r.load_config(r.CONFIG_PATH)
        import csv as _csv, io as _io
        buf = _io.StringIO()
        _csv.writer(buf, lineterminator="\n").writerows(r.sheet_template_rows(cfg))
        data = r.parse_sheet(buf.getvalue(), cfg)
        self.assertEqual(set(data.special_sessions), {"2026-09-16"})
        self.assertEqual(set(data.no_meeting), {"2026-10-13", "2026-11-25"})
        self.assertEqual(data.warnings, [])

    def test_change_alert_lists_only_changed_meetings(self):
        os.environ.pop("GITHUB_REPOSITORY", None)
        os.environ["GOOGLE_SHEET_ID"] = "SHEET123"
        try:
            before = make_config(end_date="2026-10-07")
            after = make_config(end_date="2026-10-07", unavailable={"D": ["2026-09-09"]})
            changes = r.diff_schedules(r.build_schedule(before), r.build_schedule(after), date(2026, 9, 1))
            txt = r.render_change_alert(changes, after, [])
            self.assertIn("• Wed Sep 9: C & D → *C & E* (D out)", txt)
            self.assertIn("• Tue Sep 15: E & F → *D & F*", txt)
            self.assertEqual(txt.count("•"), 2)
            self.assertIn("docs.google.com/spreadsheets/d/SHEET123", txt)
        finally:
            os.environ.pop("GOOGLE_SHEET_ID", None)

    def test_sync_sheet_dry_run_end_to_end(self):
        import io as _io, shutil, tempfile as _tf
        from contextlib import redirect_stdout
        tmp = Path(_tf.mkdtemp())
        shutil.copy(r.CONFIG_PATH, tmp / "schedule.json")
        cfg = r.load_config(tmp / "schedule.json")
        rows = r.sheet_template_rows(cfg)
        hdr = rows[0]
        for row in rows[1:]:
            if row[0] == "2026-09-23":
                row[hdr.index("Kristen")] = "out"
        import csv as _csv
        with open(tmp / "sheet.csv", "w", newline="") as fh:
            _csv.writer(fh).writerows(rows)
        old_config = r.CONFIG_PATH
        r.CONFIG_PATH = tmp / "schedule.json"
        try:
            buf = _io.StringIO()
            with redirect_stdout(buf):
                code = r.main(["sync-sheet", "--dry-run", "--csv-file", str(tmp / "sheet.csv"), "--today", "2026-09-02"])
            out = buf.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("2 upcoming meeting(s) would change", out)
            self.assertIn("Wed Sep 23: Kristen & Danyang → *Danyang & Jose* (Kristen out)", out)
            self.assertIn("Tue Sep 29: Jose & Phillip → *Kristen & Phillip*", out)
            # dry run must not touch the file
            self.assertEqual((tmp / "schedule.json").read_text(), r.CONFIG_PATH.read_text())
            self.assertEqual(json.loads((tmp / "schedule.json").read_text())["unavailable"], {})
            # real sync (no Slack) writes the file; a second run is a no-op
            buf = _io.StringIO()
            with redirect_stdout(buf):
                code = r.main(["sync-sheet", "--no-post", "--csv-file", str(tmp / "sheet.csv"), "--today", "2026-09-02"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads((tmp / "schedule.json").read_text())["unavailable"], {"Kristen": ["2026-09-23"]})
            buf = _io.StringIO()
            with redirect_stdout(buf):
                r.main(["sync-sheet", "--no-post", "--csv-file", str(tmp / "sheet.csv"), "--today", "2026-09-02"])
            self.assertIn("already up to date", buf.getvalue())
        finally:
            r.CONFIG_PATH = old_config
            shutil.rmtree(tmp)
