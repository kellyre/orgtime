"""Tests for overlap detection/resolution when editing a clock entry."""

from datetime import datetime

from orgtime.model import (
    apply_overlap_changes,
    apply_reshape,
    describe_change,
    overlap_changes,
    parse,
    reshape_to_avoid,
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


def test_describe_change_flags_wipeout():
    # other 09:30-10:00 fully inside edited 09:00-11:00 -> wiped
    doc = make(("A", "T1", 14, 15))
    other_doc = make(("B", "T2", 9, 10))
    doc.projects.append(other_doc.projects[0])
    o = doc.projects[1].tasks[0].clocks[0]
    o.start, o.end = dt(10, 9, 30), dt(10, 10)
    edited = doc.projects[0].tasks[0].clocks[0]
    changes = overlap_changes(doc, edited, dt(10, 9), dt(10, 11))
    text = describe_change(changes[0])
    assert "WIPED OUT" in text and "typo" in text


def test_others_win_trims_the_edited_entry():
    # edited 09:30-11:00 overlaps other 10:00-12:00 on the right;
    # under "others win", the edited entry trims to 09:30-10:00
    doc = make(("A", "Edit", 20, 21), ("B", "Other", 10, 12))
    edited = doc.projects[0].tasks[0].clocks[0]
    pieces = reshape_to_avoid(doc, edited, dt(10, 9, 30), dt(10, 11))
    assert pieces == [(dt(10, 9, 30), dt(10, 10))]
    apply_reshape(doc, edited, pieces, dt(10, 9, 30))
    assert (edited.start, edited.end) == (dt(10, 9, 30), dt(10, 10))
    # the other entry is untouched
    other = doc.projects[1].tasks[0].clocks[0]
    assert (other.start, other.end) == (dt(10, 10), dt(10, 12))


def test_others_win_splits_edited_around_contained_entry():
    # other 10:00-10:30 sits inside edited 09:00-12:00 -> edited splits
    doc = make(("A", "Edit", 20, 21), ("B", "Mid", 10, 11))
    edited = doc.projects[0].tasks[0].clocks[0]
    other = doc.projects[1].tasks[0].clocks[0]
    other.start, other.end = dt(10, 10), dt(10, 10, 30)
    task = doc.projects[0].tasks[0]
    pieces = reshape_to_avoid(doc, edited, dt(10, 9), dt(10, 12))
    assert pieces == [(dt(10, 9), dt(10, 10)), (dt(10, 10, 30), dt(10, 12))]
    apply_reshape(doc, edited, pieces, dt(10, 9))
    spans = sorted((c.start, c.end) for c in task.clocks)
    assert spans == [(dt(10, 9), dt(10, 10)), (dt(10, 10, 30), dt(10, 12))]


def test_others_win_wipes_edited_when_fully_covered():
    # edited 10:00-10:30 sits fully inside other 09:00-12:00 -> wiped
    doc = make(("A", "Edit", 20, 21), ("B", "Big", 9, 12))
    edited = doc.projects[0].tasks[0].clocks[0]
    pieces = reshape_to_avoid(doc, edited, dt(10, 10), dt(10, 10, 30))
    assert pieces == []  # fully covered -> wiped (likely typo)
    apply_reshape(doc, edited, pieces, dt(10, 10))
    assert edited.start == edited.end == dt(10, 10)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
