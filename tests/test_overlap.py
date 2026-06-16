"""Tests for overlap detection/resolution when editing a clock entry."""

from datetime import datetime

from orgtime.model import (
    apply_overlap_changes,
    describe_change,
    overlap_changes,
    parse,
)


def dt(day, hh, mm=0):
    return datetime(2026, 6, day, hh, mm)


def make(*intervals):
    """Build a doc; each interval is (project, task, start_hour, end_hour)."""
    lines = []
    by_project = {}
    for proj, task, s, e in intervals:
        by_project.setdefault(proj, []).append((task, s, e))
    for proj, tasks in by_project.items():
        lines.append(f"* {proj}")
        for task, s, e in tasks:
            lines.append(f"** TODO {task}")
            day = "Wed"
            lines.append(
                f"   CLOCK: [2026-06-10 {day} {s:02d}:00]"
                f"--[2026-06-10 {day} {e:02d}:00] => {e-s}:00")
    doc, issues = parse("\n".join(lines) + "\n")
    assert issues == [], issues
    return doc


def all_clocks(doc):
    return [(p, t, c) for p in doc.projects for t in p.tasks for c in t.clocks]


def test_no_overlap_no_changes():
    doc = make(("A", "T1", 9, 10), ("B", "T2", 11, 12))
    edited = doc.projects[0].tasks[0].clocks[0]
    # move A/T1 to 9:30-10:30, no conflict with 11-12
    assert overlap_changes(doc, edited, dt(10, 9, 30), dt(10, 10, 30)) == []


def test_left_overlap_trims_end():
    # other B/T2 09:00-10:00; edit A/T1 to 09:30-11:00 -> trim B end to 09:30
    doc = make(("A", "T1", 12, 13), ("B", "T2", 9, 10))
    edited = doc.projects[0].tasks[0].clocks[0]
    other = doc.projects[1].tasks[0].clocks[0]
    changes = overlap_changes(doc, edited, dt(10, 9, 30), dt(10, 11))
    assert len(changes) == 1
    c = changes[0]
    assert c.clock is other
    assert (c.new_start, c.new_end) == (dt(10, 9), dt(10, 9, 30))
    assert not c.becomes_zero and c.split_extra is None


def test_right_overlap_trims_start():
    # other 10:00-12:00; edit to 09:00-11:00 -> trim other start to 11:00
    doc = make(("A", "T1", 14, 15), ("B", "T2", 10, 12))
    edited = doc.projects[0].tasks[0].clocks[0]
    other = doc.projects[1].tasks[0].clocks[0]
    changes = overlap_changes(doc, edited, dt(10, 9), dt(10, 11))
    assert (changes[0].new_start, changes[0].new_end) == (dt(10, 11), dt(10, 12))


def test_fully_inside_collapses_to_zero():
    # other 09:30-10:00 sits fully inside edited 09:00-11:00
    doc = make(("A", "T1", 14, 15), ("B", "T2", 9, 10))
    edited = doc.projects[0].tasks[0].clocks[0]
    other = doc.projects[1].tasks[0].clocks[0]
    other.start, other.end = dt(10, 9, 30), dt(10, 10)
    changes = overlap_changes(doc, edited, dt(10, 9), dt(10, 11))
    assert len(changes) == 1
    assert changes[0].becomes_zero
    assert changes[0].new_start == changes[0].new_end == dt(10, 9, 30)


def test_container_splits_in_two():
    # other 08:00-12:00 strictly contains edited 09:00-10:00 -> split
    doc = make(("A", "T1", 14, 15), ("B", "T2", 8, 12))
    edited = doc.projects[0].tasks[0].clocks[0]
    other = doc.projects[1].tasks[0].clocks[0]
    changes = overlap_changes(doc, edited, dt(10, 9), dt(10, 10))
    assert len(changes) == 1
    c = changes[0]
    assert (c.new_start, c.new_end) == (dt(10, 8), dt(10, 9))
    assert c.split_extra == (dt(10, 10), dt(10, 12))

    # apply and confirm the task now has two non-overlapping pieces
    task = doc.projects[1].tasks[0]
    apply_overlap_changes(changes)
    spans = sorted((cl.start, cl.end) for cl in task.clocks)
    assert spans == [(dt(10, 8), dt(10, 9)), (dt(10, 10), dt(10, 12))]


def test_multiple_overlaps_across_projects():
    doc = make(
        ("A", "Edit", 20, 21),     # the entry we will edit
        ("B", "Left", 9, 10),      # left overlap
        ("C", "Right", 11, 13),    # right overlap
        ("D", "Around", 8, 14),    # container -> split
    )
    edited = doc.projects[0].tasks[0].clocks[0]
    changes = overlap_changes(doc, edited, dt(10, 9, 30), dt(10, 11, 30))
    # Left, Right, Around all conflict
    labels = {f"{c.project.name}/{c.task.name}" for c in changes}
    assert labels == {"B/Left", "C/Right", "D/Around"}
    apply_overlap_changes(changes)
    # nothing (except the edited entry, applied separately) overlaps the slot
    for p, t, c in all_clocks(doc):
        if c is edited:
            continue
        if c.start == c.end:
            continue
        assert not (c.start < dt(10, 11, 30) and dt(10, 9, 30) < c.end), \
            f"{p.name}/{t.name} still overlaps"


def test_running_and_zero_length_ignored():
    doc = make(("A", "T1", 14, 15))
    edited = doc.projects[0].tasks[0].clocks[0]
    # add a running clock and a zero-length clock in another project
    other = make(("B", "T2", 9, 9))  # zero length
    doc.projects.append(other.projects[0])
    from orgtime.model import ClockEntry
    running_task = doc.projects[0].tasks[0]
    running_task.clocks.append(ClockEntry(start=dt(10, 9, 30)))  # running
    changes = overlap_changes(doc, edited, dt(10, 9), dt(10, 12))
    assert changes == []  # zero-length and running entries are skipped


def test_describe_change_readable():
    doc = make(("A", "T1", 14, 15), ("B", "T2", 8, 12))
    edited = doc.projects[0].tasks[0].clocks[0]
    changes = overlap_changes(doc, edited, dt(10, 9), dt(10, 10))
    text = describe_change(changes[0])
    assert "B / T2" in text and "split into" in text


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
