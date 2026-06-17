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

from datetime import datetime

from orgtime import curses_app
from orgtime.curses_app import CursesApp
from orgtime.model import ClockEntry, Task, parse
from orgtime.view import CommentRef

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
        clock_a = task.clocks[0]
        select(app, clock_a)
        # edit end 10:00 -> 11:30 (overlaps Temp B); confirm the fix with 'y'
        KEYS.extend(["\n"]                                   # accept start
                    + ["\x15"] + chars("2026-07-01 11:30") + ["\n"]  # new end
                    + ["y"])                                 # confirm overlap fix
        app.handle_key("e")
        assert clock_a.end == datetime(2026, 7, 1, 11, 30)
        # Temp B was trimmed at the start to remove the overlap
        assert task_b.clocks[0].start == datetime(2026, 7, 1, 11, 30)
        assert task_b.clocks[0].end == datetime(2026, 7, 1, 12, 0)
        # cancelling instead leaves everything unchanged
        select(app, clock_a)
        KEYS.extend(["\n"]                                   # accept start
                    + ["\x15"] + chars("2026-07-01 11:45") + ["\n"]  # new end
                    + ["n"])                                 # decline the fix
        app.handle_key("e")
        assert clock_a.end == datetime(2026, 7, 1, 11, 30)   # unchanged
        assert task_b.clocks[0].start == datetime(2026, 7, 1, 11, 30)
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
        assert task.tombstones == ["### first", "### second"]

        # -- soft delete the whole task ----------------------------------
        select(app, task)
        KEYS.append("y")
        app.handle_key("d")
        assert project.tasks == []
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

        # file on disk round-trips cleanly
        doc, issues = parse(path.read_text(encoding="utf-8"))
        assert issues == [], issues


def main():
    curses.wrapper(scenario)
    print("PASS curses headless end-to-end test")


if __name__ == "__main__":
    main()
