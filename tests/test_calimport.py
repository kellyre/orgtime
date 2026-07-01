"""Tests for the calendar-CSV import core (parse + plan)."""

import tempfile
from datetime import date, datetime
from pathlib import Path

from orgtime.calimport import CalEvent, parse_csv, plan_import
from orgtime.model import make_backup

# the sample export (note the stray space before "Start Date" is tolerated)
SAMPLE = (
    "Subject,  Start Date,Start Time,End Date,End Time,All day event,"
    "Meeting Organizer,Show time as\n"
    'CTOP Dashboard,6/4/2026,2:00:00 PM,6/4/2026,3:00:00 PM,FALSE,"Kelly, Reed",2\n'
    'CTOP Dashboard,6/5/2026,2:00:00 PM,6/5/2026,3:00:00 PM,FALSE,"Kelly, Reed",2\n'
    'Maine,6/5/2026,12:00:00 AM,6/6/2026,12:00:00 AM,TRUE,"Kelly, Reed",4\n'
)


def test_parse_sample():
    events, issues = parse_csv(SAMPLE)
    assert issues == []
    assert len(events) == 3
    e0 = events[0]
    assert e0.subject == "CTOP Dashboard"
    assert e0.start == datetime(2026, 6, 4, 14, 0)
    assert e0.end == datetime(2026, 6, 4, 15, 0)
    assert e0.all_day is False and e0.status == 2
    maine = events[2]
    assert maine.all_day is True and maine.status == 4
    assert maine.start == datetime(2026, 6, 5, 0, 0)
    assert maine.end == datetime(2026, 6, 6, 0, 0)


def test_missing_required_column():
    events, issues = parse_csv("Subject,Start Date\nx,6/4/2026\n")
    assert events == []
    assert any("missing required column" in i for i in issues)


def test_plan_blackout_masks_and_allday_not_imported():
    events, _ = parse_csv(SAMPLE)
    # import the whole range 6/4..6/6 with blackout code 4
    plan = plan_import(events, date(2026, 6, 4), date(2026, 6, 6), blackout_code=4)
    subjects = [(e.subject, e.start.date()) for e in plan.candidates]
    # only CTOP 6/4 survives: CTOP 6/5 is masked by the all-day Maine (status 4),
    # and Maine itself is all-day so it is a window only, never a candidate
    assert subjects == [("CTOP Dashboard", date(2026, 6, 4))]
    assert [e.subject for e in plan.ignored_blackout] == ["CTOP Dashboard"]
    assert plan.ignored_blackout[0].start.date() == date(2026, 6, 5)
    assert (datetime(2026, 6, 5, 0, 0), datetime(2026, 6, 6, 0, 0)) in plan.windows


def test_plan_timed_blackout_is_imported_and_masks_others():
    events, _ = parse_csv(
        "Subject,Start Date,Start Time,End Date,End Time,All day event,Show time as\n"
        "Focus,6/4/2026,1:00:00 PM,6/4/2026,4:00:00 PM,FALSE,4\n"        # timed blackout
        "Standup,6/4/2026,2:00:00 PM,6/4/2026,2:30:00 PM,FALSE,2\n"      # inside blackout
        "Review,6/4/2026,5:00:00 PM,6/4/2026,6:00:00 PM,FALSE,2\n"       # clear
    )
    plan = plan_import(events, date(2026, 6, 4), date(2026, 6, 4), blackout_code=4)
    subjects = [e.subject for e in plan.candidates]
    assert subjects == ["Focus", "Review"]           # timed blackout imported
    assert [e.subject for e in plan.ignored_blackout] == ["Standup"]


def test_plan_date_range_by_start_and_ordering():
    events, _ = parse_csv(
        "Subject,Start Date,Start Time,End Date,End Time,Show time as\n"
        "Late,6/6/2026,9:00:00 AM,6/6/2026,9:30:00 AM,2\n"
        "Early,6/4/2026,9:00:00 AM,6/4/2026,9:30:00 AM,2\n"
        "Mid,6/5/2026,9:00:00 AM,6/5/2026,9:30:00 AM,2\n"
    )
    plan = plan_import(events, date(2026, 6, 4), date(2026, 6, 5), blackout_code=4)
    # 6/6 excluded by range; candidates sorted chronologically
    assert [e.subject for e in plan.candidates] == ["Early", "Mid"]


def test_configurable_blackout_code():
    events, _ = parse_csv(
        "Subject,Start Date,Start Time,End Date,End Time,All day event,Show time as\n"
        "OOF,6/5/2026,12:00:00 AM,6/6/2026,12:00:00 AM,TRUE,3\n"
        "Meet,6/5/2026,10:00:00 AM,6/5/2026,11:00:00 AM,FALSE,2\n"
    )
    # with blackout=3 the OOF masks the meeting; with blackout=4 it does not
    p3 = plan_import(events, date(2026, 6, 5), date(2026, 6, 5), blackout_code=3)
    assert [e.subject for e in p3.candidates] == []
    p4 = plan_import(events, date(2026, 6, 5), date(2026, 6, 5), blackout_code=4)
    assert [e.subject for e in p4.candidates] == ["Meet"]


def test_make_backup():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "timelog.org"
        path.write_text("* Proj\n", encoding="utf-8")
        backup = make_backup(path)
        assert backup is not None and backup.exists()
        assert backup.parent.name == "backups"
        assert backup.read_text(encoding="utf-8") == "* Proj\n"
        assert make_backup(Path(tmp) / "nope.org") is None  # nothing to back up


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
