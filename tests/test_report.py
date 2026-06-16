"""Tests for the plain-text report generator."""

from datetime import date, datetime, timedelta

from orgtime.model import parse, parse_user_date
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


if __name__ == "__main__":
    for fn in [test_parse_user_date, test_collect_all,
               test_collect_range_filters_by_start_date,
               test_effective_range_and_filename, test_build_report_contents,
               test_running_clock_noted_and_counted, test_day_form_for_large_totals]:
        fn()
        print(f"PASS {fn.__name__}")
