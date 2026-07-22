"""Tests for the small-overlap half-hour snap feature."""

from datetime import datetime

from orgtime.model import (
    ClockEntry,
    Document,
    Project,
    Task,
    apply_snap_fix,
    describe_snap_fix,
    find_snap_fixes,
    half_hour_snap_point,
)


def dt(hh, mm):
    return datetime(2026, 6, 10, hh, mm)


def make_doc(*pairs):
    """Build a doc from (project_name, task_name, start_hh, start_mm,
    end_hh, end_mm) tuples, one task per tuple, one clock each."""
    doc = Document()
    for pname, tname, sh, sm, eh, em in pairs:
        project = next((p for p in doc.projects if p.name == pname), None)
        if project is None:
            project = Project(name=pname)
            doc.projects.append(project)
        task = Task(name=tname)
        task.clocks.append(ClockEntry(start=dt(sh, sm), end=dt(eh, em)))
        project.tasks.append(task)
    return doc


def test_half_hour_snap_point_borders_left():
    # overlap [9:00, 9:07] borders the mark at its left edge
    assert half_hour_snap_point(dt(9, 0), dt(9, 7)) == dt(9, 0)


def test_half_hour_snap_point_borders_right():
    # overlap [14:28, 14:30] borders the mark at its right edge
    assert half_hour_snap_point(dt(14, 28), dt(14, 30)) == dt(14, 30)


def test_half_hour_snap_point_contains():
    # overlap [8:58, 9:04] strictly contains 9:00
    assert half_hour_snap_point(dt(8, 58), dt(9, 4)) == dt(9, 0)
    # overlap [14:28, 14:33] strictly contains 14:30
    assert half_hour_snap_point(dt(14, 28), dt(14, 33)) == dt(14, 30)


def test_half_hour_snap_point_none_when_no_mark():
    # overlap [9:18, 9:22] contains neither 9:00 nor 9:30
    assert half_hour_snap_point(dt(9, 18), dt(9, 22)) is None


def test_find_snap_fixes_borders_case():
    # A/T1 09:00-09:07 overlaps B/T2 08:00-09:00? no overlap there; use a
    # genuine small overlap: A ends 9:07, B starts 9:00 -> overlap [9,9:07]
    doc = make_doc(
        ("A", "T1", 8, 0, 9, 7),
        ("B", "T2", 9, 0, 10, 0),
    )
    fixes = find_snap_fixes(doc)
    assert len(fixes) == 1
    fix = fixes[0]
    assert fix.point == dt(9, 0)
    assert fix.clock_a.end == dt(9, 7) and fix.clock_b.start == dt(9, 0)

    apply_snap_fix(fix)
    assert fix.clock_a.end == dt(9, 0)
    assert fix.clock_b.start == dt(9, 0)
    # no longer overlapping
    assert fix.clock_a.end <= fix.clock_b.start


def test_find_snap_fixes_contains_case():
    doc = make_doc(
        ("A", "T1", 8, 0, 9, 4),
        ("B", "T2", 8, 58, 10, 0),
    )
    fixes = find_snap_fixes(doc)
    assert len(fixes) == 1
    assert fixes[0].point == dt(9, 0)


def test_no_fix_when_overlap_too_large():
    # 25 minute overlap -> not a candidate even though it contains 9:00
    doc = make_doc(
        ("A", "T1", 8, 0, 9, 15),
        ("B", "T2", 8, 50, 10, 0),
    )
    assert find_snap_fixes(doc) == []


def test_no_fix_when_no_half_hour_in_overlap():
    doc = make_doc(
        ("A", "T1", 8, 0, 9, 22),
        ("B", "T2", 9, 18, 10, 0),
    )
    assert find_snap_fixes(doc) == []


def test_no_fix_when_touching_not_overlapping():
    doc = make_doc(
        ("A", "T1", 8, 0, 9, 0),
        ("B", "T2", 9, 0, 10, 0),
    )
    assert find_snap_fixes(doc) == []


def test_no_fix_for_running_clocks():
    doc = make_doc(("A", "T1", 8, 0, 9, 7))
    doc.projects[0].tasks.append(Task(name="T2"))
    doc.projects[0].tasks[1].clocks.append(ClockEntry(start=dt(9, 0), end=None))
    assert find_snap_fixes(doc) == []


def test_describe_snap_fix_readable():
    doc = make_doc(
        ("Proj A", "T1", 8, 0, 9, 7),
        ("Proj B", "T2", 9, 0, 10, 0),
    )
    fix = find_snap_fixes(doc)[0]
    text = describe_snap_fix(fix)
    assert "Proj A / T1" in text and "Proj B / T2" in text
    assert "09:00" in text


def test_multiple_fixes_across_document():
    doc = make_doc(
        ("A", "T1", 8, 0, 9, 4),
        ("A", "T2", 8, 58, 10, 0),
        ("B", "T3", 13, 27, 14, 30),
        ("B", "T4", 14, 25, 15, 0),
    )
    fixes = find_snap_fixes(doc)
    points = sorted(f.point for f in fixes)
    assert points == [dt(9, 0), dt(14, 30)]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
