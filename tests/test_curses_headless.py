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

from orgtime import curses_app
from orgtime.curses_app import CursesApp
from orgtime.model import parse
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

        # -- new project -------------------------------------------------
        # prompt name; multiline desc (ctrl+g empty); choice priority (enter)
        KEYS.extend(chars("Website") + ["\n", ctrl("G"), "\n"])
        app.handle_key("N")
        assert len(app.doc.projects) == 1
        project = app.doc.projects[0]
        assert project.name == "Website" and project.priority == 3

        # -- new task (name, desc, priority pick '1', status default) -----
        select(app, project)
        KEYS.extend(chars("Mockups") + ["\n", ctrl("G"), "1", "\n", "\n"])
        app.handle_key("n")
        task = project.tasks[0]
        assert task.name == "Mockups" and task.priority == 1
        assert task.status == "TODO"
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
        # newline typed between the two lines (Enter), saved with Ctrl+G
        select(app, task)
        KEYS.extend(chars("first") + ["\n"] + chars("second") + ["\x07"])
        app.handle_key("m")
        assert task.comments == ["first", "second"], task.comments
        app.draw()

        # -- regression: insert a newline in the MIDDLE of existing text --
        # start "helloworld", Home, Right x5 (between hello|world), Enter, save
        KEYS.extend([curses.KEY_HOME] + [curses.KEY_RIGHT] * 5 + ["\n", "\x07"])
        out = app.edit_multiline("split test", "helloworld")
        assert out == "hello\nworld", repr(out)
        # and Backspace at column 0 joins lines back together
        KEYS.extend([curses.KEY_HOME, curses.KEY_BACKSPACE, "\x07"])
        out = app.edit_multiline("join test", "hello\nworld")
        assert out == "helloworld", repr(out)

        # -- status cycle + priority key ---------------------------------
        select(app, task)
        app.handle_key("t")  # IN-PROGRESS -> HOLD
        assert task.status == "HOLD"
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
        app.handle_key("\x12")  # Ctrl+R as get_wch delivers it
        assert app.doc.projects[0].tasks == []
        KEYS.append("y")
        app.handle_key("X")
        assert app.doc.tombstone_count() == 0

        # -- consistency report renders & closes -------------------------
        KEYS.append("q")  # any key closes the report
        app.handle_key("c")
        app.draw()

        # -- write a time report -----------------------------------------
        # clock some time first so the report has content
        select(app, app.doc.projects[0])
        KEYS.extend(chars("Task") + ["\n", "\x07", "\n", "\n"])
        app.handle_key("n")
        rtask = app.doc.projects[0].tasks[0]
        select(app, rtask)
        app.handle_key("i")
        app.handle_key("o")
        # report: blank start (open), blank end (open), accept default filename
        KEYS.extend(["\n", "\n", "\n"])
        app.handle_key("R")
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
