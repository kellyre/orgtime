"""End-to-end test of the Textual UI driving a real (headless) App via
Textual's own test harness (``App.run_test()`` + ``Pilot``).

Mirrors ``tests/test_curses_headless.py`` scenario-for-scenario: the
behaviour under test (model/view logic) is identical, only the driving
mechanism differs (scripted curses keys -> ``pilot.press(...)``).  Run:
python tests/test_textual_headless.py
"""

import asyncio
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path

from orgtime.config import load_config
from orgtime.model import (
    ClockEntry,
    EXPAND_COLLAPSED,
    EXPAND_FULL,
    EXPAND_PARTIAL,
    Project,
    Task,
    parse,
)
from orgtime.textual_app import OrgtimeApp, RowList, _row_style
from orgtime.view import CLOCK, COMMENT, MORE, PROJECT, CommentRef, MoreRef, priority_rows

SAMPLE_CSV = (
    "Subject,  Start Date,Start Time,End Date,End Time,All day event,"
    "Meeting Organizer,Show time as\n"
    'CTOP Dashboard,6/4/2026,2:00:00 PM,6/4/2026,3:00:00 PM,FALSE,"Kelly, Reed",2\n'
    'CTOP Dashboard,6/5/2026,2:00:00 PM,6/5/2026,3:00:00 PM,FALSE,"Kelly, Reed",2\n'
    'Maine,6/5/2026,12:00:00 AM,6/6/2026,12:00:00 AM,TRUE,"Kelly, Reed",4\n'
)

REVERSE_ORDER_CSV = (
    "Subject,Start Date,Start Time,End Date,End Time,Show time as\n"
    "Foo,6/2/2026,9:00:00 AM,6/2/2026,10:00:00 AM,2\n"
    "Foo,6/5/2026,9:00:00 AM,6/5/2026,10:00:00 AM,2\n"
)


async def type_text(pilot, text: str) -> None:
    for ch in text:
        await pilot.press(ch if ch != " " else "space")


async def press(pilot, *keys) -> None:
    for k in keys:
        await pilot.press(k)
        await pilot.pause()


def select(app: OrgtimeApp, obj) -> None:
    for i, row in enumerate(app.rows):
        data = row.obj
        if data is obj or (isinstance(data, (CommentRef, MoreRef))
                           and data.owner is obj):
            app.query_one("#main-list", RowList).highlighted = i
            return
    raise AssertionError(f"object not in rows: {obj!r}")


def comment_row_index(app: OrgtimeApp, owner) -> int:
    for i, row in enumerate(app.rows):
        if row.kind == COMMENT and row.obj.owner is owner:
            return i
    raise AssertionError(f"no comment row for {owner!r}")


async def _scenario(tmp_path: Path) -> None:
    path = tmp_path / "timelog.org"
    app = OrgtimeApp(path)
    async with app.run_test() as pilot:
        await pilot.pause()

        # -- new project (just type the name) ----------------------------
        await press(pilot, "N")
        await type_text(pilot, "Website")
        await press(pilot, "enter")
        assert len(app.doc.projects) == 1
        project = app.doc.projects[0]
        assert project.name == "Website" and project.priority == 3

        # -- new task (name only; defaults TODO #3) ----------------------
        select(app, project)
        await press(pilot, "n")
        await type_text(pilot, "Mockups")
        await press(pilot, "enter")
        task = project.tasks[0]
        assert task.name == "Mockups" and task.priority == 3
        assert task.status == "TODO"
        await press(pilot, "1")
        assert task.priority == 1

        # -- clock in / out ----------------------------------------------
        select(app, task)
        await press(pilot, "i")
        assert task.running_clock() is not None
        assert task.status == "IN-PROGRESS"
        await press(pilot, "o")
        assert app.doc.running() is None
        assert len(task.clocks) == 1 and not task.clocks[0].running

        # -- comment on the task via the real multi-line editor ----------
        select(app, task)
        await press(pilot, "c")
        await type_text(pilot, "first")
        await press(pilot, "enter")
        await type_text(pilot, "second")
        await press(pilot, "ctrl+o")
        assert task.comments == ["first", "second"], task.comments

        # -- regression: insert a newline in the MIDDLE of existing text,
        # and Backspace at column 0 joins lines back together.  Exercised
        # live through the comment editor on the (empty) project comment,
        # a clean slate -- TextArea's own editing is Textual's tested
        # code, not ours, so this is just a sanity check of the wiring. --
        select(app, project)
        await press(pilot, "c")
        await type_text(pilot, "helloworld")
        await press(pilot, "home")
        for _ in range(5):
            await press(pilot, "right")
        await press(pilot, "enter")
        await press(pilot, "ctrl+o")
        assert project.comments == ["hello", "world"], project.comments
        select(app, project)
        await press(pilot, "c")
        await press(pilot, "down", "backspace")
        await press(pilot, "ctrl+o")
        assert project.comments == ["helloworld"], project.comments
        select(app, project)
        await press(pilot, "c")
        await press(pilot, "end")
        for _ in range(len("helloworld")):
            await pilot.press("backspace")
        await press(pilot, "ctrl+o")  # blank it back out again
        assert project.comments == []

        # -- status cycle (both directions) + priority key ---------------
        select(app, task)
        await press(pilot, "s")  # IN-PROGRESS -> HOLD
        assert task.status == "HOLD"
        await press(pilot, "S")  # HOLD -> IN-PROGRESS (reverse)
        assert task.status == "IN-PROGRESS"
        await press(pilot, "2")
        assert task.priority == 2

        # -- timed clock-out path (edit_clock dialog) --------------------
        select(app, task)
        await press(pilot, "i")  # start a clock to edit
        clock = task.running_clock()
        select(app, clock)
        await press(pilot, "e")
        await press(pilot, "enter")  # accept prefilled start
        await type_text(pilot, "2099-01-01 10:00")
        await press(pilot, "enter")
        assert clock.end is not None and clock.end.year == 2099
        assert "WARNING" in app.message

        # -- editing a time resolves overlaps with other entries ---------
        task.clocks.clear()
        task.clocks.append(
            ClockEntry(start=datetime(2026, 7, 1, 9, 0),
                       end=datetime(2026, 7, 1, 10, 0)))
        project.tasks.append(Task(name="Temp B"))
        task_b = project.tasks[-1]
        task_b.clocks.append(
            ClockEntry(start=datetime(2026, 7, 1, 11, 0),
                       end=datetime(2026, 7, 1, 12, 0)))
        app.doc.save()
        app.refresh_rows()
        clock_b = task_b.clocks[0]
        clock_a = task.clocks[0]

        # (1) EDITED WINS
        select(app, clock_a)
        await press(pilot, "e")
        await press(pilot, "enter")
        await press(pilot, "ctrl+u")
        await type_text(pilot, "2026-07-01 11:30")
        await press(pilot, "enter")
        await press(pilot, "e")
        assert clock_a.end == datetime(2026, 7, 1, 11, 30)
        assert clock_b.start == datetime(2026, 7, 1, 11, 30)
        assert clock_b.end == datetime(2026, 7, 1, 12, 0)

        # (2) OTHERS WIN
        select(app, clock_a)
        await press(pilot, "e")
        await press(pilot, "enter")
        await press(pilot, "ctrl+u")
        await type_text(pilot, "2026-07-01 11:45")
        await press(pilot, "enter")
        await press(pilot, "o")
        assert clock_a.end == datetime(2026, 7, 1, 11, 30)
        assert clock_b.start == datetime(2026, 7, 1, 11, 30)
        assert clock_b.end == datetime(2026, 7, 1, 12, 0)

        # (3) CANCEL
        select(app, clock_a)
        await press(pilot, "e")
        await press(pilot, "enter")
        await press(pilot, "ctrl+u")
        await type_text(pilot, "2026-07-01 11:50")
        await press(pilot, "enter")
        await press(pilot, "escape")
        assert clock_a.end == datetime(2026, 7, 1, 11, 30)
        assert clock_b.start == datetime(2026, 7, 1, 11, 30)
        project.tasks.remove(task_b)
        app.doc.save()
        app.refresh_rows()

        # -- jump to running CLOCK + collapse all ------------------------
        select(app, task)
        await press(pilot, "i")
        _, _, running_clock = app.doc.running()
        await press(pilot, "C")
        assert all(p.collapsed for p in app.doc.projects)
        await press(pilot, "J")
        assert app.selected_obj() is running_clock
        assert app.doc.project_of(task).collapsed is False
        assert task.expand == EXPAND_FULL

        # -- mark DONE closes the running clock --------------------------
        select(app, task)
        await press(pilot, "D")
        assert task.status == "DONE"
        assert app.doc.running() is None

        # -- project-level DONE marks all tasks (after confirm) ----------
        select(app, task)
        await press(pilot, "S")  # DONE -> CANCELLED
        assert task.status == "CANCELLED"
        select(app, project)
        await press(pilot, "D")
        await press(pilot, "y")
        assert task.status == "DONE"

        # -- search across projects, tasks, and comments -----------------
        await press(pilot, "N")
        await type_text(pilot, "Other")
        await press(pilot, "enter")
        other = app.doc.projects[-1]
        assert other.name == "Other"

        await press(pilot, "slash")
        await type_text(pilot, "Oth")
        await press(pilot, "enter")
        assert app.selected_item() is other

        await press(pilot, "slash")
        await press(pilot, "ctrl+u")
        await type_text(pilot, "Mock")
        await press(pilot, "enter")
        assert app.selected_item() is task

        await press(pilot, "slash")
        await press(pilot, "ctrl+u")
        await type_text(pilot, "first")
        await press(pilot, "enter")
        sel = app.selected_obj()
        assert isinstance(sel, CommentRef) and sel.owner is task

        # -- move the task to another project, then back -----------------
        select(app, task)
        await press(pilot, "m")
        await press(pilot, "enter")  # only "Other" -> accept
        assert task in other.tasks and task not in project.tasks
        select(app, task)
        await press(pilot, "m")
        await press(pilot, "enter")  # only the original -> accept
        assert task in project.tasks and task not in other.tasks

        # -- soft delete the comment block -------------------------------
        select(app, task)
        app.query_one("#main-list", RowList).highlighted = comment_row_index(app, task)
        await press(pilot, "d")
        await press(pilot, "y")
        assert task.comments == []
        assert task.tombstones[0].startswith("## :DELETED: [")
        assert task.tombstones[1:] == ["### first", "### second"]

        # -- soft delete the whole task ----------------------------------
        select(app, task)
        await press(pilot, "d")
        await press(pilot, "y")
        assert project.tasks == []
        assert project.tombstones[0].startswith("## :DELETED: [")
        assert any(s.startswith("## ** ") for s in project.tombstones)
        assert app.doc.tombstone_count() > 0

        # -- undo brings the task back -----------------------------------
        await press(pilot, "u")
        assert len(app.doc.projects[0].tasks) == 1

        # -- redo, then expunge ------------------------------------------
        await press(pilot, "U")
        assert app.doc.projects[0].tasks == []
        await press(pilot, "X")
        await press(pilot, "y")
        assert app.doc.tombstone_count() == 0

        # -- consistency report renders & closes -------------------------
        await press(pilot, "v")
        await press(pilot, "q")  # any key closes the report

        # -- write a time report -----------------------------------------
        select(app, app.doc.projects[0])
        await press(pilot, "n")
        await type_text(pilot, "Task")
        await press(pilot, "enter")
        rtask = app.doc.projects[0].tasks[0]
        select(app, rtask)
        await press(pilot, "i")
        await press(pilot, "o")
        await press(pilot, "r")
        await press(pilot, "enter")  # blank start -> open
        await press(pilot, "enter")  # blank end -> open
        await press(pilot, "enter")  # accept default filename
        assert "Wrote report to" in app.message
        report_file = Path(app.message.split("Wrote report to", 1)[1].strip())
        assert report_file.exists()
        body = report_file.read_text(encoding="utf-8")
        assert "orgtime time report" in body
        assert "By project" in body and "By day" in body

        # -- created/modified bookkeeping + sort -------------------------
        assert app.doc.projects[0].modified == rtask.modified

        assert app.sort_mode == "file"
        await press(pilot, "z")
        assert app.sort_mode == "priority"
        await press(pilot, "z")
        assert app.sort_mode == "created"
        await press(pilot, "z")
        assert app.sort_mode == "modified"
        first_proj = next(r.obj for r in app.rows if r.kind == PROJECT)
        assert first_proj is max(app.doc.projects, key=lambda p: p.modified)
        await press(pilot, "z")
        assert app.sort_mode == "file"

        # edit the created time via e: keep name, set date
        select(app, app.doc.projects[0])
        await press(pilot, "e")
        await press(pilot, "enter")  # keep name
        await press(pilot, "ctrl+u")
        await type_text(pilot, "2020-01-01 08:00")
        await press(pilot, "enter")
        assert app.doc.projects[0].created == datetime(2020, 1, 1, 8, 0)

        # file on disk round-trips cleanly
        doc, issues = parse(path.read_text(encoding="utf-8"))
        assert issues == [], issues


async def _scenario_import(tmp_path: Path) -> None:
    path = tmp_path / "timelog.org"
    csv_path = tmp_path / "cal.csv"
    csv_path.write_text(SAMPLE_CSV, encoding="utf-8")
    app = OrgtimeApp(path)
    async with app.run_test() as pilot:
        await pilot.pause()
        now = app._now()
        proj = Project(name="Work", created=now, modified=now)
        task = Task(name="Meetings", created=now, modified=now)
        proj.tasks.append(task)
        app.doc.projects.append(proj)
        app.doc.save()
        app.refresh_rows()

        await press(pilot, "A")
        await type_text(pilot, str(csv_path))
        await press(pilot, "enter")   # csv path
        await press(pilot, "enter")   # blackout code = 4
        await press(pilot, "enter")   # start blank -> all
        await press(pilot, "enter")   # end blank -> all
        await press(pilot, "k")       # keep this entry
        await press(pilot, "enter")   # "+ New project" (top)
        await type_text(pilot, "Calendar")
        await press(pilot, "enter")
        await press(pilot, "enter")   # "+ New task" (top)
        await press(pilot, "enter")   # accept subject as task name

        cal = next(p for p in app.doc.projects if p.name == "Calendar")
        ctask = cal.tasks[0]
        assert ctask.name == "CTOP Dashboard"
        assert len(ctask.clocks) == 1, [c.start for c in ctask.clocks]
        c = ctask.clocks[0]
        assert c.start == datetime(2026, 6, 4, 14, 0)
        assert c.end == datetime(2026, 6, 4, 15, 0)
        assert "1 added" in app.message
        assert task.clocks == []

        assert list((path.parent / "backups").glob("timelog_*.org"))

        await press(pilot, "A")
        await type_text(pilot, str(csv_path))
        await press(pilot, "enter")
        await press(pilot, "enter")
        await press(pilot, "enter")
        await press(pilot, "enter")
        assert len(ctask.clocks) == 1
        assert "1 duplicate" in app.message


async def _scenario_import_reverse_order(tmp_path: Path) -> None:
    path = tmp_path / "timelog.org"
    csv_path = tmp_path / "reverse.csv"
    csv_path.write_text(REVERSE_ORDER_CSV, encoding="utf-8")
    app = OrgtimeApp(path)
    async with app.run_test() as pilot:
        await pilot.pause()
        now = app._now()
        proj = Project(name="Work", created=now, modified=now)
        task = Task(name="Meetings", created=now, modified=now)
        proj.tasks.append(task)
        app.doc.projects.append(proj)
        app.doc.save()
        app.refresh_rows()

        await press(pilot, "A")
        await type_text(pilot, str(csv_path))
        await press(pilot, "enter")
        await press(pilot, "enter")
        await press(pilot, "enter")
        await press(pilot, "enter")
        await press(pilot, "k")       # 1st prompt: keep
        await press(pilot, "down")
        await press(pilot, "enter")   # project: Work
        await press(pilot, "down")
        await press(pilot, "enter")   # task: Meetings
        await press(pilot, "s")       # 2nd prompt: skip

        assert len(task.clocks) == 1, [c.start for c in task.clocks]
        assert task.clocks[0].start == datetime(2026, 6, 5, 9, 0)
        assert "1 added" in app.message and "1 skipped" in app.message


async def _scenario_snap(tmp_path: Path) -> None:
    path = tmp_path / "timelog.org"
    app = OrgtimeApp(path)
    async with app.run_test() as pilot:
        await pilot.pause()
        proj = Project(name="P")
        task_a = Task(name="A")
        task_b = Task(name="B")
        task_a.clocks.append(ClockEntry(start=datetime(2026, 6, 10, 8, 0),
                                        end=datetime(2026, 6, 10, 9, 7)))
        task_b.clocks.append(ClockEntry(start=datetime(2026, 6, 10, 9, 0),
                                        end=datetime(2026, 6, 10, 10, 0)))
        proj.tasks.extend([task_a, task_b])
        app.doc.projects.append(proj)
        app.doc.save()
        app.refresh_rows()

        await press(pilot, "v")
        await press(pilot, "q")

        await press(pilot, "H")
        await press(pilot, "n")
        assert task_a.clocks[0].end == datetime(2026, 6, 10, 9, 7)

        await press(pilot, "H")
        await press(pilot, "y")
        assert task_a.clocks[0].end == datetime(2026, 6, 10, 9, 0)
        assert task_b.clocks[0].start == datetime(2026, 6, 10, 9, 0)
        assert "Snapped 1" in app.message

        await press(pilot, "H")
        assert "No small overlaps" in app.message


async def _scenario_priority_mode(tmp_path: Path) -> None:
    path = tmp_path / "timelog.org"
    app = OrgtimeApp(path)
    async with app.run_test() as pilot:
        await pilot.pause()
        p1 = Project(name="P1", priority=1, collapsed=False)
        p2 = Project(name="P2", priority=2, collapsed=True)
        task_a = Task(name="A", priority=1)
        task_b = Task(name="B", priority=2)
        p1.tasks.extend([task_a, task_b])
        task_c = Task(name="C", priority=1)
        p2.tasks.append(task_c)
        app.doc.projects.extend([p1, p2])
        app.doc.save()
        app.refresh_rows()

        def names():
            return [r.obj.name for r in priority_rows(app.doc, app._now())]

        assert names() == ["A", "C", "B"]

        await press(pilot, "p")
        await press(pilot, "j")
        await press(pilot, "i")
        assert len(app.screen_stack) == 1
        assert p2.collapsed is False
        assert task_c.clocks and task_c.clocks[-1].running
        assert app.selected_obj() is task_c.clocks[-1]
        assert task_c.status == "IN-PROGRESS"
        assert names() == ["A", "C", "B"]

        await press(pilot, "p")
        await press(pilot, "5")
        await press(pilot, "D")
        await press(pilot, "q")
        assert len(app.screen_stack) == 1
        assert task_a.status == "DONE"
        assert names() == ["C", "B"]

        for proj in app.doc.projects:
            for task in proj.tasks:
                task.status = "DONE"
        await press(pilot, "p")
        assert len(app.screen_stack) == 1
        assert "No open tasks" in app.message


async def _scenario_restore(tmp_path: Path) -> None:
    from orgtime.model import soft_delete_lines

    path = tmp_path / "timelog.org"
    app = OrgtimeApp(path)
    async with app.run_test() as pilot:
        await pilot.pause()
        proj = Project(name="P", collapsed=False)
        task_a = Task(name="A")
        task_a.clocks.append(ClockEntry(start=datetime(2026, 6, 1, 9, 0),
                                        end=datetime(2026, 6, 1, 10, 0)))
        task_b = Task(name="B", expand=EXPAND_FULL)
        dead_clock = ClockEntry(start=datetime(2026, 6, 2, 9, 0),
                                end=datetime(2026, 6, 2, 10, 0))
        proj.tasks.extend([task_a, task_b])
        app.doc.projects.append(proj)

        proj.tombstones.extend(
            soft_delete_lines(task_a.lines(), datetime(2026, 6, 3, 8, 0)))
        proj.tasks.remove(task_a)
        task_b.tombstones.extend(
            soft_delete_lines(dead_clock.lines(), datetime(2026, 6, 3, 9, 0)))
        app.doc.save()
        app.refresh_rows()

        await press(pilot, "R")
        await press(pilot, "escape")
        assert app.message == "Restore cancelled"
        assert task_b.clocks == [] and not any(t.name == "A" for t in proj.tasks)

        await press(pilot, "R")
        await press(pilot, "enter")
        assert len(task_b.clocks) == 1
        assert task_b.clocks[0].start == datetime(2026, 6, 2, 9, 0)
        assert app.selected_obj() is task_b.clocks[0]

        await press(pilot, "R")
        await press(pilot, "enter")
        assert any(t.name == "A" for t in proj.tasks)
        restored_task = next(t for t in proj.tasks if t.name == "A")
        assert len(restored_task.clocks) == 1
        assert app.selected_obj() is restored_task
        assert proj.tombstones == [] and task_b.tombstones == []

        await press(pilot, "R")
        assert "No deleted items" in app.message


async def _scenario_staleness(tmp_path: Path) -> None:
    path = tmp_path / "timelog.org"
    app = OrgtimeApp(path)
    async with app.run_test() as pilot:
        await pilot.pause()
        now = app._now()

        fresh_proj = Project(name="Fresh", modified=now)
        fresh_task = Task(name="FreshTask", modified=now)
        fresh_proj.tasks.append(fresh_task)

        stale_proj = Project(name="Stale", modified=now - timedelta(days=20))
        stale_task = Task(name="StaleTask", modified=now - timedelta(days=8))
        stale_proj.tasks.append(stale_task)

        ancient_proj = Project(name="Ancient", modified=now - timedelta(days=200))
        ancient_task = Task(name="AncientTask", modified=now - timedelta(days=200))
        ancient_proj.tasks.append(ancient_task)

        app.doc.projects.extend([fresh_proj, stale_proj, ancient_proj])
        for p in app.doc.projects:
            p.collapsed = False
        app.doc.save()
        app.refresh_rows()

        def row_for(obj):
            return next(r for r in app.rows if r.obj is obj)

        assert row_for(fresh_proj).stale == "fresh"
        assert row_for(stale_proj).stale == "stale"
        assert row_for(ancient_proj).stale == "ancient"
        assert row_for(fresh_task).stale == "fresh"
        assert row_for(stale_task).stale == "stale"
        assert row_for(ancient_task).stale == "ancient"

        fresh_style = _row_style(row_for(fresh_task))
        stale_style = _row_style(row_for(stale_task))
        ancient_style = _row_style(row_for(ancient_task))
        assert len({fresh_style, stale_style, ancient_style}) == 3


async def _scenario_timeline(tmp_path: Path) -> None:
    path = tmp_path / "timelog.org"
    app = OrgtimeApp(path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.config.workday_start == 7 and app.config.workday_end == 18
        now = app._now()
        proj = Project(name="Work", created=now, modified=now)
        task = Task(name="Meetings", created=now, modified=now)
        proj.tasks.append(task)
        app.doc.projects.append(proj)
        today = date.today()
        task.clocks.append(ClockEntry(
            start=datetime.combine(today, time(5, 0)),
            end=datetime.combine(today, time(5, 30))))
        app.doc.save()
        app.refresh_rows()

        await press(pilot, "t")
        await press(pilot, "a")
        await press(pilot, "ctrl+u")
        await type_text(pilot, "09:00")
        await press(pilot, "enter")
        await press(pilot, "ctrl+u")
        await type_text(pilot, "10:00")
        await press(pilot, "enter")
        await press(pilot, "down")
        await press(pilot, "enter")   # project: Work
        await press(pilot, "down")
        await press(pilot, "enter")   # task: Meetings

        await press(pilot, "less_than_sign")
        await press(pilot, "less_than_sign")
        await press(pilot, "greater_than_sign")
        await press(pilot, "R")
        await press(pilot, "less_than_sign")
        await press(pilot, "greater_than_sign")
        await press(pilot, "W")
        await press(pilot, "q")

        assert len(task.clocks) == 2, [c.start for c in task.clocks]
        added = task.clocks[1]
        assert added.start == datetime.combine(today, time(9, 0))
        assert added.end == datetime.combine(today, time(10, 0))

        assert app.config.workday_start == 6 and app.config.workday_end == 19
        on_disk = load_config(path)
        assert on_disk.workday_start == 6 and on_disk.workday_end == 19


async def _scenario_timeline_edit_delete(tmp_path: Path) -> None:
    path = tmp_path / "timelog.org"
    app = OrgtimeApp(path)
    async with app.run_test() as pilot:
        await pilot.pause()
        today = date.today()
        proj = Project(name="Work")
        task = Task(name="Standup")
        task.clocks.append(ClockEntry(
            start=datetime.combine(today, time(9, 0)),
            end=datetime.combine(today, time(10, 0))))
        proj.tasks.append(task)
        app.doc.projects.append(proj)
        app.doc.save()
        app.refresh_rows()

        # rows: GAP(7-9), ENTRY(9-10), GAP(10-18) -- move onto the entry
        await press(pilot, "t")
        await press(pilot, "j")
        await press(pilot, "e")
        await press(pilot, "enter")   # keep the prefilled start
        await press(pilot, "ctrl+u")
        await type_text(pilot, "10:30")
        await press(pilot, "enter")
        await press(pilot, "q")
        assert task.clocks[0].end == datetime.combine(today, time(10, 30))

        await press(pilot, "t")
        await press(pilot, "j")
        await press(pilot, "d")
        await press(pilot, "y")
        await press(pilot, "q")
        assert task.clocks == []
        assert task.tombstones
        assert task.tombstones[0].startswith("## :DELETED: [")


async def _scenario_clock_in_focus(tmp_path: Path) -> None:
    path = tmp_path / "timelog.org"
    app = OrgtimeApp(path)
    async with app.run_test() as pilot:
        await pilot.pause()
        proj = Project(name="P")
        task = Task(name="A")
        proj.tasks.append(task)
        app.doc.projects.append(proj)
        app.doc.save()
        app.refresh_rows()

        select(app, task)
        await press(pilot, "i")
        assert task.clocks and task.clocks[-1].running
        assert app.selected_obj() is task.clocks[-1]

        await press(pilot, "o")

        select(app, task)
        await press(pilot, "I")
        await press(pilot, "ctrl+u")
        await type_text(pilot, "08:00")
        await press(pilot, "enter")
        assert task.clocks[-1].start.strftime("%H:%M") == "08:00"
        assert app.selected_obj() is task.clocks[-1]


async def _scenario_task_expand_cycle(tmp_path: Path) -> None:
    path = tmp_path / "timelog.org"
    app = OrgtimeApp(path)
    async with app.run_test() as pilot:
        await pilot.pause()
        proj = Project(name="P")
        task = Task(name="A", comments=["task note"])
        older = ClockEntry(start=datetime(2026, 6, 1, 9, 0),
                           end=datetime(2026, 6, 1, 10, 0),
                           comments=["older note"])
        latest = ClockEntry(start=datetime(2026, 6, 2, 9, 0),
                            end=datetime(2026, 6, 2, 10, 0))
        task.clocks.extend([older, latest])
        proj.tasks.append(task)
        app.doc.projects.append(proj)
        app.doc.save()
        app.refresh_rows()

        assert task.expand == EXPAND_COLLAPSED
        assert not any(r.kind in (CLOCK, MORE, COMMENT) for r in app.rows)

        select(app, task)
        await press(pilot, "space")
        assert task.expand == EXPAND_PARTIAL
        clock_rows = [r for r in app.rows if r.kind == CLOCK]
        more_rows = [r for r in app.rows if r.kind == MORE]
        assert len(clock_rows) == 1 and clock_rows[0].obj is latest
        assert len(more_rows) == 1 and more_rows[0].text.strip() == "... (1)"
        assert isinstance(more_rows[0].obj, MoreRef)
        assert more_rows[0].obj.owner is task

        app.query_one("#main-list", RowList).highlighted = comment_row_index(app, task)
        await press(pilot, "space")
        assert task.expand == EXPAND_FULL
        assert len([r for r in app.rows if r.kind == CLOCK]) == 2

        await press(pilot, "j")
        await press(pilot, "s")
        assert task.expand == EXPAND_FULL

        app.query_one("#main-list", RowList).highlighted = comment_row_index(app, older)
        await press(pilot, "space")
        assert task.expand == EXPAND_PARTIAL
        assert app.selected_obj() is latest

        await press(pilot, "space")  # partial -> full
        select(app, older)
        await press(pilot, "space")
        assert task.expand == EXPAND_PARTIAL
        assert app.selected_obj() is latest

        select(app, latest)
        await press(pilot, "space")
        assert task.expand == EXPAND_FULL

        await press(pilot, "space")  # full -> collapsed
        assert task.expand == EXPAND_COLLAPSED
        assert not any(r.kind in (CLOCK, MORE) for r in app.rows)
        select(app, task)
        await press(pilot, "space")  # collapsed -> partial
        assert task.expand == EXPAND_PARTIAL

        for i, row in enumerate(app.rows):
            if row.kind == MORE:
                app.query_one("#main-list", RowList).highlighted = i
                break
        else:
            raise AssertionError("no MORE row found")
        await press(pilot, "space")
        assert task.expand == EXPAND_FULL
        assert not any(r.kind == MORE for r in app.rows)

        select(app, task)
        await press(pilot, "space")
        assert task.expand == EXPAND_COLLAPSED
        assert not any(r.kind in (CLOCK, MORE) for r in app.rows)

        await press(pilot, "space")  # -> partial, MORE row exists again
        for i, row in enumerate(app.rows):
            if row.kind == MORE:
                app.query_one("#main-list", RowList).highlighted = i
                break
        await press(pilot, "d")
        assert task in proj.tasks
        await press(pilot, "e")
        assert task.name == "A"


async def _scenario_reveal_partial_not_full(tmp_path: Path) -> None:
    path = tmp_path / "timelog.org"
    app = OrgtimeApp(path)
    async with app.run_test() as pilot:
        await pilot.pause()
        proj = Project(name="P")
        task = Task(name="A")
        other = Task(name="Other")
        proj.tasks.extend([task, other])
        app.doc.projects.append(proj)
        app.doc.save()
        app.refresh_rows()

        assert task.expand == EXPAND_COLLAPSED
        select(app, task)
        await press(pilot, "i")
        assert task.expand == EXPAND_PARTIAL

        select(app, other)
        await press(pilot, "J")
        assert app.selected_obj() is task.clocks[-1]
        assert task.expand == EXPAND_PARTIAL

        await press(pilot, "space")  # partial -> full
        assert task.expand == EXPAND_FULL
        await press(pilot, "o")
        select(app, task)
        await press(pilot, "i")
        assert task.expand == EXPAND_FULL

        select(app, other)
        await press(pilot, "J")
        assert task.expand == EXPAND_FULL


async def _scenario_merge_tasks(tmp_path: Path) -> None:
    path = tmp_path / "timelog.org"
    app = OrgtimeApp(path)
    async with app.run_test() as pilot:
        await pilot.pause()
        proj = Project(name="P", collapsed=False)
        dest = Task(name="Dest", comments=["keep"])
        dest.clocks.append(ClockEntry(start=datetime(2026, 6, 1, 9, 0),
                                      end=datetime(2026, 6, 1, 10, 0)))
        src_a = Task(name="A", comments=["a note"])
        src_a.clocks.append(ClockEntry(start=datetime(2026, 6, 1, 11, 0),
                                       end=datetime(2026, 6, 1, 12, 0)))
        src_b = Task(name="B")
        proj.tasks.extend([dest, src_a, src_b])
        app.doc.projects.append(proj)
        app.doc.save()
        app.refresh_rows()

        lonely_proj = Project(name="Solo", collapsed=False)
        lonely_task = Task(name="Only")
        lonely_proj.tasks.append(lonely_task)
        app.doc.projects.append(lonely_proj)
        app.refresh_rows()
        select(app, lonely_task)
        await press(pilot, "M")
        assert "No other task" in app.message

        select(app, dest)
        await press(pilot, "M")
        await press(pilot, "escape")
        assert app.message == "Merge cancelled"
        assert [t.name for t in proj.tasks] == ["Dest", "A", "B"]

        await press(pilot, "M")
        await press(pilot, "space")   # select "A" (first option)
        await press(pilot, "enter")   # confirm checklist
        await press(pilot, "n")       # decline the "Merge ... ?" prompt
        assert [t.name for t in proj.tasks] == ["Dest", "A", "B"]

        select(app, dest)
        await press(pilot, "M")
        await press(pilot, "space")   # toggle A
        await press(pilot, "down")
        await press(pilot, "space")   # toggle B
        await press(pilot, "enter")   # confirm checklist
        await press(pilot, "y")       # confirm the merge
        assert [t.name for t in proj.tasks] == ["Dest"]
        assert dest.comments == ["keep", "a note"]
        assert [c.start for c in dest.clocks] == [
            datetime(2026, 6, 1, 9, 0), datetime(2026, 6, 1, 11, 0)]
        assert app.selected_obj() is dest
        assert len(proj.tombstones) > 0


async def _scenario_modal_lists_are_alphabetical(tmp_path: Path) -> None:
    path = tmp_path / "timelog.org"
    app = OrgtimeApp(path)
    async with app.run_test() as pilot:
        await pilot.pause()
        zeta = Project(name="Zeta", collapsed=False)
        alpha = Project(name="Alpha", collapsed=False)
        mid = Project(name="Mid", collapsed=False)
        task = Task(name="T")
        zeta.tasks.append(task)
        app.doc.projects.extend([zeta, alpha, mid])
        app.doc.save()
        app.refresh_rows()

        select(app, task)
        await press(pilot, "m")
        await press(pilot, "enter")   # first option -> Alpha
        assert task in alpha.tasks

        select(app, task)
        await press(pilot, "m")
        await press(pilot, "enter")   # Mid, Zeta -> Mid
        assert task in mid.tasks

        dest = Task(name="Dest")
        zulu = Task(name="Zulu")
        echo = Task(name="Echo")
        mid.tasks.extend([dest, zulu, echo])
        app.refresh_rows()
        select(app, dest)
        await press(pilot, "M")
        await press(pilot, "space")   # first option -> Echo
        await press(pilot, "enter")
        await press(pilot, "y")
        assert echo not in mid.tasks and dest.comments == []
        assert zulu in mid.tasks

        zulu2 = Task(name="Zulu2")
        beta2 = Task(name="Beta2")
        alpha.tasks.extend([zulu2, beta2])
        app.refresh_rows()
        csv_path = tmp_path / "cal.csv"
        csv_path.write_text(SAMPLE_CSV, encoding="utf-8")
        await press(pilot, "A")
        await type_text(pilot, str(csv_path))
        await press(pilot, "enter")
        await press(pilot, "enter")
        await press(pilot, "enter")
        await press(pilot, "enter")
        await press(pilot, "k")
        await press(pilot, "down")
        await press(pilot, "enter")   # project: skip "+New" -> Alpha
        await press(pilot, "down")
        await press(pilot, "enter")   # task: skip "+New" -> Beta2
        assert beta2.clocks and not zulu2.clocks


async def _scenario_open_note(tmp_path: Path) -> None:
    path = tmp_path / "timelog.org"
    notes_dir = tmp_path / "vault"
    app = OrgtimeApp(path)
    async with app.run_test() as pilot:
        await pilot.pause()
        proj = Project(name="Website", collapsed=False)
        task = Task(name="Design mockups")
        proj.tasks.append(task)
        app.doc.projects.append(proj)
        app.doc.save()
        app.refresh_rows()

        launched = []
        app._launch_editor = lambda p: launched.append(p) or True

        assert app.config.notes_dir == ""
        select(app, task)
        await press(pilot, "V")
        await type_text(pilot, str(notes_dir))
        await press(pilot, "enter")
        assert app.config.notes_dir == str(notes_dir)
        dest = notes_dir / "Design mockups.md"
        assert launched == [dest]
        assert "Opened note" in app.message

        text = dest.read_text(encoding="utf-8")
        lines = text.splitlines()
        assert lines[0] == "# Design mockups"
        assert lines[2] == "Project: Website"
        assert lines[3] == "Task: Design mockups"
        assert text.endswith("\n\n")

        cfg2 = load_config(path)
        assert cfg2.notes_dir == str(notes_dir)

        dest.write_text("MY EDITS\n", encoding="utf-8")
        select(app, task)
        await press(pilot, "V")
        assert launched == [dest, dest]
        assert dest.read_text(encoding="utf-8") == "MY EDITS\n"

        select(app, proj)
        await press(pilot, "V")
        await press(pilot, "down")
        await press(pilot, "enter")   # project chooser: Website
        await press(pilot, "enter")   # "+ New task"
        await type_text(pilot, "Research doc")
        await press(pilot, "enter")
        new_task = next(t for t in proj.tasks if t.name == "Research doc")
        assert app.selected_obj() is new_task
        new_dest = notes_dir / "Research doc.md"
        assert launched[-1] == new_dest
        assert new_dest.exists()
        assert "Project: Website" in new_dest.read_text(encoding="utf-8")


SCENARIOS = [
    _scenario,
    _scenario_import,
    _scenario_import_reverse_order,
    _scenario_snap,
    _scenario_priority_mode,
    _scenario_restore,
    _scenario_staleness,
    _scenario_timeline,
    _scenario_timeline_edit_delete,
    _scenario_clock_in_focus,
    _scenario_task_expand_cycle,
    _scenario_reveal_partial_not_full,
    _scenario_merge_tasks,
    _scenario_modal_lists_are_alphabetical,
    _scenario_open_note,
]


async def run_all() -> None:
    for scenario in SCENARIOS:
        with tempfile.TemporaryDirectory() as tmp:
            await scenario(Path(tmp))


def main() -> None:
    asyncio.run(run_all())
    print("PASS textual headless end-to-end test")


if __name__ == "__main__":
    main()
