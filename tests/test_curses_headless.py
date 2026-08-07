"""End-to-end test of the curses UI driving REAL curses.

Runs under ``curses.wrapper`` so every window, the ``textpad`` multi-line
editor, and colour handling are exercised for real on this platform.  Only
the keyboard *input source* is faked: ``curses.newwin`` is wrapped so each
window (and stdscr) pulls keystrokes from a scripted queue instead of
blocking on the console.

Requires the ``curses`` module (stdlib on Unix, ``windows-curses`` on
Windows).  Run:  python tests/test_curses_headless.py
"""

import curses
import tempfile
from collections import deque
from pathlib import Path

from datetime import date, datetime, time

from orgtime import curses_app
from orgtime.curses_app import CursesApp
from orgtime.model import ClockEntry, Project, Task, parse

SAMPLE_CSV = (
    "Subject,  Start Date,Start Time,End Date,End Time,All day event,"
    "Meeting Organizer,Show time as\n"
    'CTOP Dashboard,6/4/2026,2:00:00 PM,6/4/2026,3:00:00 PM,FALSE,"Kelly, Reed",2\n'
    'CTOP Dashboard,6/5/2026,2:00:00 PM,6/5/2026,3:00:00 PM,FALSE,"Kelly, Reed",2\n'
    'Maine,6/5/2026,12:00:00 AM,6/6/2026,12:00:00 AM,TRUE,"Kelly, Reed",4\n'
)

# two non-overlapping same-subject events on different dates, used to prove
# the interactive import walks candidates newest-first
REVERSE_ORDER_CSV = (
    "Subject,Start Date,Start Time,End Date,End Time,Show time as\n"
    "Foo,6/2/2026,9:00:00 AM,6/2/2026,10:00:00 AM,2\n"
    "Foo,6/5/2026,9:00:00 AM,6/5/2026,10:00:00 AM,2\n"
)

from orgtime.view import PROJECT, CommentRef

KEYS: deque = deque()


class WinProxy:
    """Delegates everything to a real curses window except key input,
    which is served from the shared scripted ``KEYS`` queue."""

    def __init__(self, win):
        self._win = win

    def __getattr__(self, name):
        return getattr(self._win, name)

    def _next(self):
        if not KEYS:
            raise RuntimeError("scripted key queue exhausted")
        return KEYS.popleft()

    def get_wch(self):           # used by App loop and line/choice dialogs
        return self._next()

    def getch(self):             # used by curses.textpad.Textbox
        ch = self._next()
        return ord(ch) if isinstance(ch, str) else ch


def ctrl(c: str) -> int:
    return ord(c) - 64  # 'G' -> 7


def chars(s: str):
    return list(s)


def select(app, obj):
    for i, row in enumerate(app.rows):
        data = row.obj
        if data is obj or (isinstance(data, CommentRef) and data.owner is obj):
            app.cursor = i
            return
    raise AssertionError(f"object not in rows: {obj!r}")


def scenario(stdscr):
    real_newwin = curses.newwin
    curses.newwin = lambda *a, **k: WinProxy(real_newwin(*a, **k))
    try:
        _scenario(WinProxy(stdscr))
    finally:
        curses.newwin = real_newwin


def _scenario(stdscr):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "timelog.org"
        app = CursesApp(stdscr, path)
        curses.curs_set(0)
        app._init_colors()
        app.refresh_rows()
        app.draw()

        # -- new project (just type the name) ----------------------------
        KEYS.extend(chars("Website") + ["\n"])
        app.handle_key("N")
        assert len(app.doc.projects) == 1
        project = app.doc.projects[0]
        assert project.name == "Website" and project.priority == 3

        # -- new task (name only; defaults TODO #3) ----------------------
        select(app, project)
        KEYS.extend(chars("Mockups") + ["\n"])
        app.handle_key("n")
        task = project.tasks[0]
        assert task.name == "Mockups" and task.priority == 3
        assert task.status == "TODO"
        # priority is set afterward with a digit key
        app.handle_key("1")
        assert task.priority == 1
        app.draw()

        # -- clock in / out ----------------------------------------------
        select(app, task)
        app.handle_key("i")
        assert task.running_clock() is not None
        assert task.status == "IN-PROGRESS"
        app.handle_key("o")
        assert app.doc.running() is None
        assert len(task.clocks) == 1 and not task.clocks[0].running

        # -- comment on the task via the real multi-line editor ----------
        # newline typed between the two lines (Enter), saved with Ctrl+O
        select(app, task)
        KEYS.extend(chars("first") + ["\n"] + chars("second") + ["\x0f"])
        app.handle_key("c")
        assert task.comments == ["first", "second"], task.comments
        app.draw()

        # -- regression: insert a newline in the MIDDLE of existing text --
        # start "helloworld", Home, Right x5 (between hello|world), Enter, save
        KEYS.extend([curses.KEY_HOME] + [curses.KEY_RIGHT] * 5 + ["\n", "\x0f"])
        out = app.edit_multiline("split test", "helloworld")
        assert out == "hello\nworld", repr(out)
        # and Backspace at column 0 joins lines back together
        KEYS.extend([curses.KEY_HOME, curses.KEY_BACKSPACE, "\x0f"])
        out = app.edit_multiline("join test", "hello\nworld")
        assert out == "helloworld", repr(out)

        # -- status cycle (both directions) + priority key ---------------
        select(app, task)
        app.handle_key("s")  # IN-PROGRESS -> HOLD
        assert task.status == "HOLD"
        app.handle_key("S")  # HOLD -> IN-PROGRESS (reverse)
        assert task.status == "IN-PROGRESS"
        app.handle_key("2")
        assert task.priority == 2

        # -- timed clock-out path (edit_clock dialog) --------------------
        select(app, task)
        app.handle_key("i")  # start a clock to edit
        clock = task.running_clock()
        select(app, clock)
        # edit_clock: start prompt (accept), end prompt (type a time)
        KEYS.extend(["\n"] + chars("2099-01-01 10:00") + ["\n"])
        app.handle_key("e")
        assert clock.end is not None and clock.end.year == 2099
        # a future end is implausible -> warning surfaced
        assert "WARNING" in app.message

        # -- editing a time resolves overlaps with other entries ---------
        # clean, deterministic setup: task A 09:00-10:00, temp task B 11-12
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

        # (1) EDITED WINS: end 10:00 -> 11:30 overlaps Temp B; choose 'e'
        select(app, clock_a)
        KEYS.extend(["\n"]                                   # accept start
                    + ["\x15"] + chars("2026-07-01 11:30") + ["\n"]  # new end
                    + ["e"])                                 # edited time wins
        app.handle_key("e")
        assert clock_a.end == datetime(2026, 7, 1, 11, 30)
        # Temp B was trimmed at the start to remove the overlap
        assert clock_b.start == datetime(2026, 7, 1, 11, 30)
        assert clock_b.end == datetime(2026, 7, 1, 12, 0)

        # (2) OTHERS WIN: end -> 11:45 overlaps Temp B; choose 'o'
        select(app, clock_a)
        KEYS.extend(["\n"]
                    + ["\x15"] + chars("2026-07-01 11:45") + ["\n"]
                    + ["o"])                                 # other entries win
        app.handle_key("e")
        # the edited entry was trimmed back; Temp B is untouched
        assert clock_a.end == datetime(2026, 7, 1, 11, 30)
        assert clock_b.start == datetime(2026, 7, 1, 11, 30)
        assert clock_b.end == datetime(2026, 7, 1, 12, 0)

        # (3) CANCEL: Esc leaves everything unchanged
        select(app, clock_a)
        KEYS.extend(["\n"]
                    + ["\x15"] + chars("2026-07-01 11:50") + ["\n"]
                    + ["\x1b"])                              # cancel
        app.handle_key("e")
        assert clock_a.end == datetime(2026, 7, 1, 11, 30)   # unchanged
        assert clock_b.start == datetime(2026, 7, 1, 11, 30)
        # remove the temp task to restore the scenario state
        project.tasks.remove(task_b)
        app.doc.save()
        app.refresh_rows()

        # -- jump to running CLOCK + collapse all ------------------------
        select(app, task)
        app.handle_key("i")  # start a running clock on the task
        _, _, running_clock = app.doc.running()
        app.handle_key("C")  # collapse all projects
        assert all(p.collapsed for p in app.doc.projects)
        app.handle_key("J")  # jump straight to the open CLOCK line
        assert app.selected_obj() is running_clock
        assert app.doc.project_of(task).collapsed is False
        assert task.collapsed is False

        # -- mark DONE closes the running clock --------------------------
        select(app, task)
        app.handle_key("D")
        assert task.status == "DONE"
        assert app.doc.running() is None

        # -- project-level DONE marks all tasks (after confirm) ----------
        select(app, task)
        app.handle_key("S")  # DONE -> CANCELLED (reverse), so we can see it change
        assert task.status == "CANCELLED"
        select(app, project)
        KEYS.append("y")     # confirm the project-wide DONE
        app.handle_key("D")
        assert task.status == "DONE"

        # -- search across projects, tasks, and comments -----------------
        # add a second project to search for and move into
        KEYS.extend(chars("Other") + ["\n"])
        app.handle_key("N")
        other = app.doc.projects[-1]
        assert other.name == "Other"

        # find the second project by substring (term box starts empty)
        KEYS.extend(chars("Oth") + ["\n"])
        app.handle_key("/")
        assert app.selected_item() is other

        # next search (Ctrl+U clears the prefilled term) -> the task name
        KEYS.extend(["\x15"] + chars("Mock") + ["\n"])
        app.handle_key("/")
        assert app.selected_item() is task

        # search a comment substring -> lands on a comment row of the task
        KEYS.extend(["\x15"] + chars("first") + ["\n"])
        app.handle_key("/")
        sel = app.selected_obj()
        assert isinstance(sel, CommentRef) and sel.owner is task

        # -- move the task to another project, then back -----------------
        select(app, task)
        KEYS.append("\n")  # only other project is "Other" -> accept
        app.handle_key("m")
        assert task in other.tasks and task not in project.tasks
        select(app, task)
        KEYS.append("\n")  # now the only other project is the original
        app.handle_key("m")
        assert task in project.tasks and task not in other.tasks

        # -- soft delete the comment block -------------------------------
        select(app, task)
        # move cursor onto a comment row of this task
        for i, row in enumerate(app.rows):
            if isinstance(row.obj, CommentRef) and row.obj.owner is task:
                app.cursor = i
                break
        KEYS.append("y")  # confirm
        app.handle_key("d")
        assert task.comments == []
        # tombstoned comments are prefixed with a :DELETED: marker (used by
        # find_deleted_items/report inclusion) ahead of the ### lines
        assert task.tombstones[0].startswith("## :DELETED: [")
        assert task.tombstones[1:] == ["### first", "### second"]

        # -- soft delete the whole task ----------------------------------
        select(app, task)
        KEYS.append("y")
        app.handle_key("d")
        assert project.tasks == []
        assert project.tombstones[0].startswith("## :DELETED: [")
        assert any(s.startswith("## ** ") for s in project.tombstones)
        assert app.doc.tombstone_count() > 0

        # -- undo brings the task back -----------------------------------
        app.handle_key("u")
        assert len(app.doc.projects[0].tasks) == 1

        # -- redo, then expunge ------------------------------------------
        app.handle_key("U")  # redo
        assert app.doc.projects[0].tasks == []
        KEYS.append("y")
        app.handle_key("X")
        assert app.doc.tombstone_count() == 0

        # -- consistency report renders & closes -------------------------
        KEYS.append("q")  # any key closes the report
        app.handle_key("v")  # verify (consistency check)
        app.draw()

        # -- write a time report -----------------------------------------
        # clock some time first so the report has content
        select(app, app.doc.projects[0])
        KEYS.extend(chars("Task") + ["\n"])
        app.handle_key("n")
        rtask = app.doc.projects[0].tasks[0]
        select(app, rtask)
        app.handle_key("i")
        app.handle_key("o")
        # report: blank start (open), blank end (open), accept default filename
        KEYS.extend(["\n", "\n", "\n"])
        app.handle_key("r")
        assert "Wrote report to" in app.message
        report_file = Path(app.message.split("Wrote report to", 1)[1].strip())
        assert report_file.exists()
        body = report_file.read_text(encoding="utf-8")
        assert "orgtime time report" in body
        assert "By project" in body and "By day" in body

        # -- created/modified bookkeeping + sort -------------------------
        # creating + clocking the task bubbled modified up to its project
        assert app.doc.projects[0].modified == rtask.modified

        # sort cycles file -> priority -> created -> modified -> file
        assert app.sort_mode == "file"
        app.handle_key("z")
        assert app.sort_mode == "priority"
        app.handle_key("z")
        assert app.sort_mode == "created"
        app.handle_key("z")
        assert app.sort_mode == "modified"
        # newest-modified project is shown first
        first_proj = next(r.obj for r in app.rows if r.kind == PROJECT)
        assert first_proj is max(app.doc.projects, key=lambda p: p.modified)
        app.handle_key("z")
        assert app.sort_mode == "file"  # back to file order

        # edit the created time via e (project created late): keep name, set date
        select(app, app.doc.projects[0])
        KEYS.extend(["\n"]                                    # keep name
                    + ["\x15"] + chars("2020-01-01 08:00") + ["\n"])  # created
        app.handle_key("e")
        assert app.doc.projects[0].created == datetime(2020, 1, 1, 8, 0)

        # file on disk round-trips cleanly
        doc, issues = parse(path.read_text(encoding="utf-8"))
        assert issues == [], issues


def _scenario_import(stdscr):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "timelog.org"
        csv_path = Path(tmp) / "cal.csv"
        csv_path.write_text(SAMPLE_CSV, encoding="utf-8")
        app = CursesApp(stdscr, path)
        curses.curs_set(0)
        app._init_colors()
        # pre-create a project + task to import into
        now = app._now()
        proj = Project(name="Work", created=now, modified=now)
        task = Task(name="Meetings", created=now, modified=now)
        proj.tasks.append(task)
        app.doc.projects.append(proj)
        app.doc.save()
        app.refresh_rows()

        # import: csv path, blackout code (default 4), blank start/end (= all),
        # then for CTOP 6/4: keep -> "+ New project" (top) -> "+ New task" (top)
        KEYS.extend(chars(str(csv_path)) + ["\n"])   # csv path
        KEYS.extend(["\n"])                           # blackout code = 4
        KEYS.extend(["\n", "\n"])                     # start/end blank -> all
        KEYS.extend(["k"])                            # keep this entry
        KEYS.extend(["\n"] + chars("Calendar") + ["\n"])  # New project (top) + name
        KEYS.extend(["\n", "\n"])                     # New task (top) + accept subject
        app.handle_key("A")
        # a new project/task was created (New is at the top of the chooser)
        cal = next(p for p in app.doc.projects if p.name == "Calendar")
        ctask = cal.tasks[0]
        assert ctask.name == "CTOP Dashboard"       # task name defaults to subject
        # only CTOP 6/4 imported: 6/5 is masked by the all-day blackout "Maine"
        assert len(ctask.clocks) == 1, [c.start for c in ctask.clocks]
        c = ctask.clocks[0]
        assert c.start == datetime(2026, 6, 4, 14, 0)
        assert c.end == datetime(2026, 6, 4, 15, 0)
        assert "1 added" in app.message
        # the pre-existing task was left alone
        assert task.clocks == []

        # a backup of the file was written under backups/
        assert list((path.parent / "backups").glob("timelog_*.org"))

        # re-import the same range -> exact duplicate is auto-skipped
        KEYS.extend(chars(str(csv_path)) + ["\n", "\n", "\n", "\n"])
        app.handle_key("A")
        assert len(ctask.clocks) == 1
        assert "1 duplicate" in app.message


def _scenario_import_reverse_order(stdscr):
    """Interactive import walks candidates newest-first: keeping the FIRST
    prompt and skipping the second should keep the 6/5 event (newer) and
    skip the 6/2 event (older) — the opposite of forward chronological order."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "timelog.org"
        csv_path = Path(tmp) / "reverse.csv"
        csv_path.write_text(REVERSE_ORDER_CSV, encoding="utf-8")
        app = CursesApp(stdscr, path)
        curses.curs_set(0)
        app._init_colors()
        now = app._now()
        proj = Project(name="Work", created=now, modified=now)
        task = Task(name="Meetings", created=now, modified=now)
        proj.tasks.append(task)
        app.doc.projects.append(proj)
        app.doc.save()
        app.refresh_rows()

        KEYS.extend(chars(str(csv_path)) + ["\n"])   # csv path
        KEYS.extend(["\n"])                           # blackout code default
        KEYS.extend(["\n", "\n"])                     # start/end blank -> all
        KEYS.extend(["k"])                            # 1st prompt: keep
        KEYS.extend([curses.KEY_DOWN, "\n"])          # project: Work
        KEYS.extend([curses.KEY_DOWN, "\n"])          # task: Meetings
        KEYS.extend(["s"])                            # 2nd prompt: skip
        app.handle_key("A")

        assert len(task.clocks) == 1, [c.start for c in task.clocks]
        assert task.clocks[0].start == datetime(2026, 6, 5, 9, 0)  # newer kept
        assert "1 added" in app.message and "1 skipped" in app.message


def _scenario_snap(stdscr):
    """H finds and fixes a small overlap bordering a half-hour mark."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "timelog.org"
        app = CursesApp(stdscr, path)
        curses.curs_set(0)
        app._init_colors()
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

        # consistency check mentions the fixable overlap
        KEYS.append("q")
        app.handle_key("v")

        # H shows the proposed fix; decline first (nothing changes)
        KEYS.append("n")
        app.handle_key("H")
        assert task_a.clocks[0].end == datetime(2026, 6, 10, 9, 7)

        # H again, accept -> both boundaries snap to 09:00
        KEYS.append("y")
        app.handle_key("H")
        assert task_a.clocks[0].end == datetime(2026, 6, 10, 9, 0)
        assert task_b.clocks[0].start == datetime(2026, 6, 10, 9, 0)
        assert "Snapped 1" in app.message

        # nothing left to snap
        app.handle_key("H")
        assert "No small overlaps" in app.message


def _scenario_priority_mode(stdscr):
    """p opens the cross-project priority triage view.

    priority_mode() is its own blocking loop (like timeline_mode()): a
    single handle_key("p") call doesn't return until 'i' or 'q' ends the
    session, so each interactive step below queues its whole key sequence
    up front. Ordering is checked between sessions via priority_rows()
    directly, decoupled from the interactive loop.
    """
    from orgtime.view import priority_rows

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "timelog.org"
        app = CursesApp(stdscr, path)
        curses.curs_set(0)
        app._init_colors()

        p1 = Project(name="P1", priority=1, collapsed=False)
        p2 = Project(name="P2", priority=2, collapsed=True)  # stays collapsed
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

        # task priority ascending, ties broken by project priority:
        # A (t1/p1), C (t1/p2), B (t2/p1)
        assert names() == ["A", "C", "B"]

        # move onto C, then i: clocks in, auto-expands its (collapsed)
        # project, and returns to normal mode with the cursor on the new
        # clock entry -- exactly as if 'i' had been pressed on C there
        KEYS.extend(["j", "i"])
        app.handle_key("p")
        assert app.mode == "normal"
        assert p2.collapsed is False
        assert task_c.clocks and task_c.clocks[-1].running
        assert app.selected_obj() is task_c.clocks[-1]
        assert task_c.status == "IN-PROGRESS"
        assert names() == ["A", "C", "B"]  # C still open, same order

        # re-enter: change A's (selected on entry) priority to 5 (lowest);
        # the list re-sorts immediately and the cursor follows A, then D
        # marks it done and it drops out of the view, then q leaves
        KEYS.extend(["5", "D", "q"])
        app.handle_key("p")
        assert app.mode == "normal"
        assert task_a.status == "DONE"
        assert names() == ["C", "B"]

        # once nothing is open, entering the view is a no-op with a message
        for project in app.doc.projects:
            for task in project.tasks:
                task.status = "DONE"
        app.handle_key("p")
        assert app.mode == "normal"
        assert "No open tasks" in app.message


def _scenario_restore(stdscr):
    """R lists deletions (most recent first) and restores the chosen one.

    The two deletions below are set up directly (rather than via the 'd'
    key) with explicit, distinct :DELETED: timestamps -- 'd' stamps with
    real, minute-truncated wall-clock time, so two quick presses in a fast
    test could tie and make the intended ordering flaky. The interactive
    'R' flow itself (list, cancel, restore) is what's under test here.
    """
    from orgtime.model import soft_delete_lines

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "timelog.org"
        app = CursesApp(stdscr, path)
        curses.curs_set(0)
        app._init_colors()

        proj = Project(name="P", collapsed=False)
        task_a = Task(name="A")
        task_a.clocks.append(ClockEntry(start=datetime(2026, 6, 1, 9, 0),
                                        end=datetime(2026, 6, 1, 10, 0)))
        task_b = Task(name="B", collapsed=False)
        dead_clock = ClockEntry(start=datetime(2026, 6, 2, 9, 0),
                                end=datetime(2026, 6, 2, 10, 0))
        proj.tasks.extend([task_a, task_b])
        app.doc.projects.append(proj)

        # task A deleted first (older), then B's clock (more recent)
        proj.tombstones.extend(
            soft_delete_lines(task_a.lines(), datetime(2026, 6, 3, 8, 0)))
        proj.tasks.remove(task_a)
        task_b.tombstones.extend(
            soft_delete_lines(dead_clock.lines(), datetime(2026, 6, 3, 9, 0)))
        app.doc.save()
        app.refresh_rows()

        # R lists both, most-recently-deleted first: the clock before the task
        KEYS.append("\x1b")  # Esc: back out first, to prove cancel is a no-op
        app.handle_key("R")
        assert app.message == "Restore cancelled"
        assert task_b.clocks == [] and not any(t.name == "A" for t in proj.tasks)

        # restore the clock (top of the list): index 0, Enter
        KEYS.append("\n")
        app.handle_key("R")
        assert len(task_b.clocks) == 1
        assert task_b.clocks[0].start == datetime(2026, 6, 2, 9, 0)
        assert app.selected_obj() is task_b.clocks[0]

        # restore the task: now the only entry left, index 0, Enter
        KEYS.append("\n")
        app.handle_key("R")
        assert any(t.name == "A" for t in proj.tasks)
        restored_task = next(t for t in proj.tasks if t.name == "A")
        assert len(restored_task.clocks) == 1
        assert app.selected_obj() is restored_task
        assert proj.tombstones == [] and task_b.tombstones == []

        # nothing left to restore
        app.handle_key("R")
        assert "No deleted items" in app.message


def _scenario_staleness(stdscr):
    """Row.stale is computed from `modified` and changes the drawn colour."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "timelog.org"
        app = CursesApp(stdscr, path)
        curses.curs_set(0)
        app._init_colors()
        now = app._now()
        from datetime import timedelta

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
        app.draw()  # exercise the real drawing/colour path, not just logic

        def row_for(obj):
            return next(r for r in app.rows if r.obj is obj)

        assert row_for(fresh_proj).stale == "fresh"
        assert row_for(stale_proj).stale == "stale"
        assert row_for(ancient_proj).stale == "ancient"
        assert row_for(fresh_task).stale == "fresh"
        assert row_for(stale_task).stale == "stale"
        assert row_for(ancient_task).stale == "ancient"

        # each tier renders with a visibly different attribute
        fresh_attr = app._row_attr(row_for(fresh_task))
        stale_attr = app._row_attr(row_for(stale_task))
        ancient_attr = app._row_attr(row_for(ancient_task))
        assert len({fresh_attr, stale_attr, ancient_attr}) == 3


def _scenario_timeline(stdscr):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "timelog.org"
        app = CursesApp(stdscr, path)
        curses.curs_set(0)
        app._init_colors()
        assert app.config.workday_start == 7 and app.config.workday_end == 18
        now = app._now()
        proj = Project(name="Work", created=now, modified=now)
        task = Task(name="Meetings", created=now, modified=now)
        proj.tasks.append(task)
        app.doc.projects.append(proj)
        # an entry before the default 7-18 window, to exercise the
        # hidden-entries hint (drawn for real, not just unit-tested)
        today = date.today()
        task.clocks.append(ClockEntry(
            start=datetime.combine(today, time(5, 0)),
            end=datetime.combine(today, time(5, 30))))
        app.doc.save()
        app.refresh_rows()

        # timeline opens on today; add 09:00-10:00 into the one big gap,
        # choosing existing Work/Meetings (one row below the "+ New" top row)
        KEYS.extend(["a"]
                    + ["\x15"] + chars("09:00") + ["\n"]   # explicit start
                    + ["\x15"] + chars("10:00") + ["\n"]   # explicit end
                    + [curses.KEY_DOWN, "\n"]              # project: Work
                    + [curses.KEY_DOWN, "\n"])             # task: Meetings
        # widen the window, reset it, widen again, then save as the default
        KEYS.extend(["<", "<", ">", "R"])                  # widen then reset (no-op'd)
        KEYS.extend(["<", ">", "W"])                       # widen once each way, save
        KEYS.extend(["q"])                                 # leave timeline
        app.handle_key("t")

        assert len(task.clocks) == 2, [c.start for c in task.clocks]
        added = task.clocks[1]
        assert added.start == datetime.combine(today, time(9, 0))
        assert added.end == datetime.combine(today, time(10, 0))

        # R reset the widen before saving, so the persisted default is the
        # original 7/18 widened by exactly one hour each way -> 6/19
        assert app.config.workday_start == 6 and app.config.workday_end == 19
        from orgtime.config import load_config
        on_disk = load_config(path)
        assert on_disk.workday_start == 6 and on_disk.workday_end == 19


def _scenario_clock_in_focus(stdscr):
    """i (and I) move the cursor onto the freshly created clock entry."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "timelog.org"
        app = CursesApp(stdscr, path)
        curses.curs_set(0)
        app._init_colors()
        proj = Project(name="P")
        task = Task(name="A")
        proj.tasks.append(task)
        app.doc.projects.append(proj)
        app.doc.save()
        app.refresh_rows()

        select(app, task)
        app.handle_key("i")
        assert task.clocks and task.clocks[-1].running
        assert app.selected_obj() is task.clocks[-1]

        app.handle_key("o")  # clock back out so I can clock in again below

        select(app, task)
        KEYS.extend(["\x15"] + chars("08:00") + ["\n"])
        app.handle_key("I")
        assert task.clocks[-1].start.strftime("%H:%M") == "08:00"
        assert app.selected_obj() is task.clocks[-1]


def main():
    def run_all(stdscr):
        real_newwin = curses.newwin
        curses.newwin = lambda *a, **k: WinProxy(real_newwin(*a, **k))
        try:
            _scenario(WinProxy(stdscr))
            KEYS.clear()
            _scenario_import(WinProxy(stdscr))
            KEYS.clear()
            _scenario_import_reverse_order(WinProxy(stdscr))
            KEYS.clear()
            _scenario_snap(WinProxy(stdscr))
            KEYS.clear()
            _scenario_clock_in_focus(WinProxy(stdscr))
            KEYS.clear()
            _scenario_priority_mode(WinProxy(stdscr))
            KEYS.clear()
            _scenario_restore(WinProxy(stdscr))
            KEYS.clear()
            _scenario_staleness(WinProxy(stdscr))
            KEYS.clear()
            _scenario_timeline(WinProxy(stdscr))
        finally:
            curses.newwin = real_newwin
    curses.wrapper(run_all)
    print("PASS curses headless end-to-end test")


if __name__ == "__main__":
    main()
