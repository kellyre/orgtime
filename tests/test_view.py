"""Tests for the curses-free view layer (flatten / labels / collapse)."""

from datetime import datetime

from orgtime.model import Document, parse
from orgtime.view import (
    CLOCK,
    COMMENT,
    PROJECT,
    TASK,
    CommentRef,
    flatten,
    next_match_index,
    search_targets,
)

SAMPLE = """\
* [#2] Website Redesign
# project note
** IN-PROGRESS [#1] Design mockups
# task note
   CLOCK: [2026-06-09 Tue 09:00]--[2026-06-09 Tue 10:30] => 1:30
# clock note
** TODO [#3] Write copy

* [#4] Admin
** DONE [#5] File taxes
   CLOCK: [2026-04-01 Wed 13:00]--[2026-04-01 Wed 15:15] => 2:15
"""

NOW = datetime(2026, 6, 11, 12, 0)


def kinds(rows):
    return [r.kind for r in rows]


def test_flatten_full_tree():
    doc, issues = parse(SAMPLE)
    assert issues == []
    # everything expanded by default (projects expanded, tasks expanded here)
    for p in doc.projects:
        for t in p.tasks:
            t.collapsed = False
    rows = flatten(doc, NOW)
    # project, its comment, task, task comment, clock, clock comment, task2,
    # project2, task, clock
    assert kinds(rows) == [
        PROJECT, COMMENT, TASK, COMMENT, CLOCK, COMMENT, TASK,
        PROJECT, TASK, CLOCK,
    ]
    # comment rows carry a CommentRef pointing at their owner
    proj_comment = rows[1]
    assert isinstance(proj_comment.obj, CommentRef)
    assert proj_comment.obj.owner is doc.projects[0]


def test_collapse_hides_descendants():
    doc, _ = parse(SAMPLE)
    doc.projects[0].collapsed = True
    rows = flatten(doc, NOW)
    # collapsed project shows only itself; second project expands but its
    # task stays collapsed (tasks default to collapsed), hiding the clock
    assert kinds(rows) == [PROJECT, PROJECT, TASK]
    doc.projects[1].tasks[0].collapsed = False
    rows = flatten(doc, NOW)
    assert kinds(rows) == [PROJECT, PROJECT, TASK, CLOCK]


def test_collapsed_task_hides_clocks_and_comments():
    doc, _ = parse(SAMPLE)
    doc.projects[0].tasks[0].collapsed = True
    rows = flatten(doc, NOW)
    labels = [r.text for r in rows]
    assert any("Design mockups" in s for s in labels)
    # its clock + comments are hidden
    assert not any("CLOCK: [2026-06-09" in s for s in labels)
    assert not any("task note" in s for s in labels)
    # collapse marker present
    task_row = next(r for r in rows if "Design mockups" in r.text)
    assert task_row.text.lstrip().startswith("+")


def test_running_and_warn_flags():
    text = """\
* P
** TODO Long
   CLOCK: [2026-06-09 Tue 08:00]--[2026-06-11 Thu 08:00] => 48:00
** TODO Live
   CLOCK: [2026-06-11 Thu 11:30]
"""
    doc, _ = parse(text)
    for t in doc.projects[0].tasks:
        t.collapsed = False
    rows = flatten(doc, NOW)
    long_clock = next(r for r in rows if r.kind == CLOCK and "48:00" in r.text)
    assert long_clock.warn  # 48h flagged
    live = next(r for r in rows if r.kind == TASK and "Live" in r.text)
    assert live.running


def test_human_duration_in_totals():
    text = """\
* P
** TODO Big
   CLOCK: [2026-06-08 Mon 00:00]--[2026-06-10 Wed 22:43] => 70:43
"""
    doc, _ = parse(text)
    rows = flatten(doc, NOW)
    project_row = rows[0]
    assert "2d 22:43" in project_row.text


def test_search_targets_order_and_kinds():
    doc, _ = parse(SAMPLE)
    targets = search_targets(doc)
    # document order: project, project comment, task, task comment, clock
    # comment, task2, project2, task...
    assert [t.kind for t in targets][:6] == [
        PROJECT, COMMENT, TASK, COMMENT, COMMENT, TASK]
    # the clock-comment target carries its task as the ancestor to expand
    clock_comment = next(t for t in targets if t.text == "clock note")
    assert clock_comment.kind == COMMENT
    assert clock_comment.task is doc.projects[0].tasks[0]
    assert clock_comment.owner is doc.projects[0].tasks[0].clocks[0]


def test_search_targets_finds_collapsed_content():
    doc, _ = parse(SAMPLE)
    doc.projects[0].collapsed = True  # hide everything under it
    targets = search_targets(doc)
    # collapse state does not affect what is searchable
    assert any(t.text == "clock note" for t in targets)
    assert any(t.text == "Design mockups" for t in targets)


def test_sorted_projects():
    from orgtime.view import sorted_projects
    from orgtime.model import Project

    a = Project(name="Alpha", priority=3,
                created=datetime(2026, 6, 1), modified=datetime(2026, 6, 10))
    b = Project(name="Beta", priority=1,
                created=datetime(2026, 6, 5), modified=datetime(2026, 6, 2))
    c = Project(name="Gamma", priority=2,
                created=datetime(2026, 6, 3), modified=datetime(2026, 6, 20))
    doc = Document(projects=[a, b, c])

    assert [p.name for p in sorted_projects(doc, "file")] == ["Alpha", "Beta", "Gamma"]
    assert [p.name for p in sorted_projects(doc, "priority")] == ["Beta", "Gamma", "Alpha"]
    assert [p.name for p in sorted_projects(doc, "created")] == ["Alpha", "Gamma", "Beta"]
    # modified: newest first
    assert [p.name for p in sorted_projects(doc, "modified")] == ["Gamma", "Alpha", "Beta"]
    # the doc's own order is untouched (view-only)
    assert [p.name for p in doc.projects] == ["Alpha", "Beta", "Gamma"]


def test_timeline_rows_gaps_and_entries():
    from datetime import date
    from orgtime.view import ENTRY, GAP, timeline_rows

    text = """\
* P
** TODO T
   CLOCK: [2026-06-15 Mon 10:00]--[2026-06-15 Mon 11:00] => 1:00
   CLOCK: [2026-06-15 Mon 13:00]--[2026-06-15 Mon 14:30] => 1:30
   CLOCK: [2026-06-16 Tue 09:30]--[2026-06-16 Tue 10:00] => 0:30
"""
    doc, _ = parse(text)
    now = datetime(2026, 6, 18, 12, 0)
    rows, ws, we = timeline_rows(doc, date(2026, 6, 15), 9, 17, now=now)
    assert (ws.hour, we.hour) == (9, 17)
    # 9-10 gap, 10-11 entry, 11-13 gap, 13-14:30 entry, 14:30-17 gap
    kinds = [(r.kind, r.start.strftime("%H:%M"), r.end.strftime("%H:%M")) for r in rows]
    assert kinds == [
        (GAP, "09:00", "10:00"),
        (ENTRY, "10:00", "11:00"),
        (GAP, "11:00", "13:00"),
        (ENTRY, "13:00", "14:30"),
        (GAP, "14:30", "17:00"),
    ]
    # the 6/16 clock is on another day and not shown
    assert all(r.start.date() == date(2026, 6, 15) for r in rows)
    # entries carry their task/project
    entry = next(r for r in rows if r.kind == ENTRY)
    assert entry.task is doc.projects[0].tasks[0]


def test_timeline_empty_day_is_one_gap():
    from datetime import date
    from orgtime.view import GAP, timeline_rows

    doc, _ = parse("* P\n** TODO T\n")
    rows, ws, we = timeline_rows(doc, date(2026, 6, 15), 9, 17,
                                 now=datetime(2026, 6, 18, 12, 0))
    assert len(rows) == 1 and rows[0].kind == GAP
    assert rows[0].start == ws and rows[0].end == we


def test_timeline_rows_default_window_is_7_to_18():
    from datetime import date
    from orgtime.view import timeline_rows

    doc, _ = parse("* P\n** TODO T\n")
    rows, ws, we = timeline_rows(doc, date(2026, 6, 15),
                                 now=datetime(2026, 6, 18, 12, 0))
    assert (ws.hour, we.hour) == (7, 18)


def test_timeline_hidden_counts():
    from datetime import date
    from orgtime.view import timeline_hidden_counts

    text = """\
* P
** TODO T
   CLOCK: [2026-06-15 Mon 06:00]--[2026-06-15 Mon 06:30] => 0:30
   CLOCK: [2026-06-15 Mon 10:00]--[2026-06-15 Mon 11:00] => 1:00
   CLOCK: [2026-06-15 Mon 19:00]--[2026-06-15 Mon 20:00] => 1:00
   CLOCK: [2026-06-16 Tue 09:00]--[2026-06-16 Tue 10:00] => 1:00
"""
    doc, _ = parse(text)
    ws = datetime(2026, 6, 15, 7, 0)
    we = datetime(2026, 6, 15, 18, 0)
    before, after = timeline_hidden_counts(doc, date(2026, 6, 15), ws, we,
                                           now=datetime(2026, 6, 18, 12, 0))
    # 06:00-06:30 is before the window; 19:00-20:00 is after; the 10-11
    # entry is inside the window (not hidden); the 6/16 entry is a different day
    assert (before, after) == (1, 1)
    # widening the window to include 6am leaves nothing hidden before
    before2, _ = timeline_hidden_counts(
        doc, date(2026, 6, 15), datetime(2026, 6, 15, 6, 0), we,
        now=datetime(2026, 6, 18, 12, 0))
    assert before2 == 0


def test_next_match_index_wraps_and_loops():
    doc, _ = parse(SAMPLE)
    targets = search_targets(doc)
    texts = [t.text for t in targets]
    # "note" matches project note, task note, clock note
    note_indices = [i for i, t in enumerate(texts) if "note" in t.lower()]
    assert len(note_indices) >= 3
    # starting from -1 gives the first; each subsequent call advances; wraps
    i1 = next_match_index(targets, "note", -1)
    i2 = next_match_index(targets, "note", i1)
    i3 = next_match_index(targets, "note", i2)
    assert [i1, i2, i3] == note_indices[:3]
    # from the last match it loops back to the first
    assert next_match_index(targets, "note", note_indices[-1]) == note_indices[0]
    # case-insensitive, and None when nothing matches
    assert next_match_index(targets, "DESIGN", -1) is not None
    assert next_match_index(targets, "zzz", -1) is None


if __name__ == "__main__":
    for fn in [test_flatten_full_tree, test_collapse_hides_descendants,
               test_collapsed_task_hides_clocks_and_comments,
               test_running_and_warn_flags, test_human_duration_in_totals,
               test_search_targets_order_and_kinds,
               test_search_targets_finds_collapsed_content,
               test_sorted_projects,
               test_timeline_rows_gaps_and_entries,
               test_timeline_empty_day_is_one_gap,
               test_timeline_rows_default_window_is_7_to_18,
               test_timeline_hidden_counts,
               test_next_match_index_wraps_and_loops]:
        fn()
        print(f"PASS {fn.__name__}")
