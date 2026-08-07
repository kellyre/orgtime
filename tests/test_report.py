"""Tests for the plain-text report generator."""

from datetime import date, datetime, timedelta

from orgtime.model import (
    ClockEntry,
    Document,
    Project,
    Task,
    parse,
    parse_user_date,
    soft_delete_lines,
)
from orgtime.report import (
    build_report,
    collect,
    default_filename,
    effective_range,
)

SAMPLE = """\
* [#2] Website
** IN-PROGRESS [#1] Design
   CLOCK: [2026-06-09 Tue 09:00]--[2026-06-09 Tue 10:30] => 1:30
   CLOCK: [2026-06-10 Wed 11:00]--[2026-06-10 Wed 12:00] => 1:00
** TODO [#3] Copy
   CLOCK: [2026-06-10 Wed 13:00]--[2026-06-10 Wed 13:30] => 0:30

* [#5] Admin
** TODO [#5] Taxes
   CLOCK: [2026-06-12 Fri 09:00]--[2026-06-12 Fri 11:00] => 2:00
"""

NOW = datetime(2026, 6, 12, 15, 0)


def test_parse_user_date():
    assert parse_user_date("2026-06-10") == date(2026, 6, 10)
    assert parse_user_date("2026/06/10") == date(2026, 6, 10)
    assert parse_user_date("nope") is None
    assert parse_user_date("2026-13-01") is None


def test_collect_all():
    doc, _ = parse(SAMPLE)
    entries, now = collect(doc, now=NOW)
    assert len(entries) == 4
    total = sum((d for *_, d in entries), timedelta())
    assert total == timedelta(hours=5)  # 1:30 + 1:00 + 0:30 + 2:00


def test_collect_range_filters_by_start_date():
    doc, _ = parse(SAMPLE)
    entries, _ = collect(doc, start=date(2026, 6, 10), end=date(2026, 6, 10),
                         now=NOW)
    # only the two 2026-06-10 entries
    assert len(entries) == 2
    total = sum((d for *_, d in entries), timedelta())
    assert total == timedelta(hours=1, minutes=30)


def test_effective_range_and_filename():
    doc, _ = parse(SAMPLE)
    assert effective_range(doc, None, None, NOW) == (
        date(2026, 6, 9), date(2026, 6, 12))
    assert default_filename(doc, None, None, NOW) == \
        "orgtime-report-20260609_20260612.txt"
    # explicit bounds are honoured in the name
    assert default_filename(doc, date(2026, 6, 10), date(2026, 6, 11), NOW) == \
        "orgtime-report-20260610_20260611.txt"


def test_build_report_contents():
    doc, _ = parse(SAMPLE)
    text = build_report(doc, now=NOW)
    assert "Total time : 5:00" in text
    assert "Entries    : 4" in text
    # by-project totals
    assert "Website" in text and "Admin" in text
    # by-project-and-task shows task with status
    assert "IN-PROGRESS Design" in text
    # by-day section has the three distinct days
    assert "2026-06-09 Tue" in text
    assert "2026-06-10 Wed" in text
    assert "2026-06-12 Fri" in text
    # Website total = 3:00 (1:30+1:00+0:30); Design = 2:30
    lines = text.splitlines()
    web = next(l for l in lines if l.startswith("Website"))
    assert web.endswith("3:00")
    design = next(l for l in lines if "Design" in l and "IN-PROGRESS" in l)
    assert design.endswith("2:30")


def test_running_clock_noted_and_counted():
    text = """\
* P
** IN-PROGRESS Live
   CLOCK: [2026-06-12 Fri 14:00]
"""
    doc, _ = parse(text)
    report = build_report(doc, now=NOW)  # NOW is 15:00, so 1:00 elapsed
    assert "Total time : 1:00" in report
    assert "running clock(s)" in report
    assert "Live (P)" in report


def test_day_form_for_large_totals():
    text = """\
* P
** TODO Big
   CLOCK: [2026-06-01 Mon 00:00]--[2026-06-03 Wed 02:00] => 50:00
"""
    doc, _ = parse(text)
    report = build_report(doc, now=NOW)
    assert "Total time : 50:00  (2d 2:00)" in report


def _deleted_doc():
    """A project with one live task/clock, one deleted clock (still under
    the live task), one deleted task (under the live project), and one
    wholly deleted project -- mirrors what curses_app.delete() produces."""
    doc = Document()

    proj = Project(name="Live Project")
    task = Task(name="Live Task")
    task.clocks.append(ClockEntry(start=datetime(2026, 6, 10, 9, 0),
                                  end=datetime(2026, 6, 10, 10, 0)))
    proj.tasks.append(task)
    doc.projects.append(proj)

    dead_clock = ClockEntry(start=datetime(2026, 6, 10, 11, 0),
                            end=datetime(2026, 6, 10, 12, 0))  # 1:00
    task.tombstones.extend(soft_delete_lines(dead_clock.lines(),
                                             datetime(2026, 6, 11, 8, 0)))

    dead_task = Task(name="Deleted Task")
    dead_task.clocks.append(ClockEntry(start=datetime(2026, 6, 10, 13, 0),
                                       end=datetime(2026, 6, 10, 15, 0)))  # 2:00
    proj.tombstones.extend(soft_delete_lines(dead_task.lines(),
                                             datetime(2026, 6, 11, 9, 0)))

    dead_proj = Project(name="Deleted Project")
    dead_proj_task = Task(name="Orphaned Task")
    dead_proj_task.clocks.append(ClockEntry(start=datetime(2026, 6, 10, 16, 0),
                                            end=datetime(2026, 6, 10, 19, 0)))  # 3:00
    dead_proj.tasks.append(dead_proj_task)
    doc.projects.append(dead_proj)
    doc.tombstones.extend(soft_delete_lines(dead_proj.lines(),
                                            datetime(2026, 6, 11, 10, 0)))
    doc.projects.remove(dead_proj)

    return doc, proj, task


def test_collect_includes_deleted_items_by_default():
    doc, proj, task = _deleted_doc()
    entries, _ = collect(doc, now=datetime(2026, 6, 12))
    # 1 live + 1 deleted-clock + 1 deleted-task-clock + 1 deleted-project-clock
    assert len(entries) == 4
    total = sum((d for *_, d in entries), timedelta())
    assert total == timedelta(hours=1 + 1 + 2 + 3)  # live + 3 deleted


def test_collect_can_exclude_deleted_items():
    doc, proj, task = _deleted_doc()
    entries, _ = collect(doc, now=datetime(2026, 6, 12), include_deleted=False)
    assert len(entries) == 1
    total = sum((d for *_, d in entries), timedelta())
    assert total == timedelta(hours=1)


def test_build_report_labels_deleted_items_but_counts_their_time():
    doc, proj, task = _deleted_doc()
    report = build_report(doc, now=datetime(2026, 6, 12))
    # grand total includes everything: 1 (live) + 1 + 2 + 3 (deleted) = 7:00
    assert "Total time : 7:00" in report

    lines = report.splitlines()
    # the still-live project is NOT labeled, and its total includes the
    # deleted clock (1:00) and deleted task (2:00) time credited to it: 4:00
    live_proj_line = next(l for l in lines
                          if l.startswith("Live Project"))
    assert live_proj_line.endswith("4:00")
    assert "(deleted) Live Project" not in report

    # the deleted task shows up labeled, indented under the live project
    assert any("(deleted) TODO Deleted Task" in l for l in lines)

    # the live task itself is NOT labeled (only its deleted CLOCK is
    # invisible/uncredited-as-such -- individual clocks never get their own
    # report line)
    assert any(l.strip().startswith("TODO Live Task") and "(deleted)" not in l
              for l in lines)

    # the wholly deleted project shows up labeled, along with its task
    assert any(l.startswith("(deleted) Deleted Project") for l in lines)
    assert any("(deleted) TODO Orphaned Task" in l for l in lines)


if __name__ == "__main__":
    for fn in [test_parse_user_date, test_collect_all,
               test_collect_range_filters_by_start_date,
               test_effective_range_and_filename, test_build_report_contents,
               test_running_clock_noted_and_counted, test_day_form_for_large_totals,
               test_collect_includes_deleted_items_by_default,
               test_collect_can_exclude_deleted_items,
               test_build_report_labels_deleted_items_but_counts_their_time]:
        fn()
        print(f"PASS {fn.__name__}")
