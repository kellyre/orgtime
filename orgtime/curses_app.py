"""Curses front-end for orgtime — a dependency-free alternative to the
Textual UI for locked-down environments.

Run with::

    python -m orgtime.curses_app [path/to/file.org]

It reuses :mod:`orgtime.model` (parsing, clocking, soft-delete, consistency
checks) and :mod:`orgtime.view` (row flattening / labels) unchanged, so the
file format and behaviour match the Textual version.  Cosmetic features that
depended on Textual (live-ticking clock, blink, rich colours) are dropped.

Note: the Python standard library ships ``curses`` on Linux/macOS but not on
Windows, so this front-end targets Unix-like systems.
"""

from __future__ import annotations

import copy
import curses
import sys
from datetime import datetime
from pathlib import Path

from .model import (
    STATUSES,
    ClockEntry,
    Document,
    Project,
    Task,
    check_consistency,
    clock_warnings,
    comment_lines,
    format_ts,
    load,
    parse_user_date,
    parse_user_ts,
    tombstoned,
)
from .report import build_report, default_filename
from .view import (
    COMMENT,
    PROJECT,
    TASK,
    CommentRef,
    HELP_LINES,
    flatten,
)

UNDO_LIMIT = 100
_CANCEL = object()  # sentinel: user pressed Esc in a prompt

# colour pair ids
CP_PROJECT = 1
CP_STATUS = 2
CP_COMMENT = 3
CP_WARN = 4
CP_RUNNING = 5
CP_BAR = 6


class CursesApp:
    def __init__(self, stdscr, path: Path) -> None:
        self.stdscr = stdscr
        self.doc, self.load_issues = load(path)
        self.rows = []
        self.cursor = 0      # index into self.rows
        self.top = 0         # first visible row (scroll offset)
        self.message = ""
        self._undo: list[Document] = []
        self._redo: list[Document] = []
        self._running = True

    # -- setup -------------------------------------------------------------

    def run(self) -> None:
        curses.curs_set(0)
        self.stdscr.keypad(True)
        self._init_colors()
        self.refresh_rows()
        if self.load_issues:
            self.show_report("Problems while loading file", self.load_issues)
        while self._running:
            self.draw()
            try:
                ch = self.stdscr.get_wch()
            except curses.error:
                continue
            self.handle_key(ch)
        self.doc.save()

    def _init_colors(self) -> None:
        self.has_color = curses.has_colors()
        if not self.has_color:
            return
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(CP_PROJECT, curses.COLOR_CYAN, -1)
        curses.init_pair(CP_STATUS, curses.COLOR_YELLOW, -1)
        curses.init_pair(CP_COMMENT, curses.COLOR_GREEN, -1)
        curses.init_pair(CP_WARN, curses.COLOR_RED, -1)
        curses.init_pair(CP_RUNNING, curses.COLOR_YELLOW, -1)
        curses.init_pair(CP_BAR, curses.COLOR_WHITE, curses.COLOR_BLUE)

    def color(self, pair: int, bold: bool = False) -> int:
        if not self.has_color:
            return curses.A_BOLD if bold else curses.A_NORMAL
        attr = curses.color_pair(pair)
        return attr | curses.A_BOLD if bold else attr

    # -- model/view sync ---------------------------------------------------

    def refresh_rows(self) -> None:
        """Rebuild the visible-row list, keeping the cursor on the same item."""
        prev = self.selected_obj()
        prev_owner = prev.owner if isinstance(prev, CommentRef) else None
        self.rows = flatten(self.doc)
        if prev is not None:
            for i, row in enumerate(self.rows):
                obj = row.obj
                if obj is prev or (
                    isinstance(obj, CommentRef) and prev_owner is not None
                    and obj.owner is prev_owner
                ):
                    self.cursor = i
                    break
        self.cursor = max(0, min(self.cursor, len(self.rows) - 1))

    def selected_obj(self):
        if 0 <= self.cursor < len(self.rows):
            return self.rows[self.cursor].obj
        return None

    def selected_item(self):
        """Selected object with CommentRef unwrapped to its owner."""
        obj = self.selected_obj()
        return obj.owner if isinstance(obj, CommentRef) else obj

    def selected_task(self) -> Task | None:
        obj = self.selected_item()
        if isinstance(obj, Task):
            return obj
        if isinstance(obj, ClockEntry):
            return self.doc.task_of(obj)
        return None

    def save_and_refresh(self) -> None:
        self.doc.save()
        self.refresh_rows()

    # -- drawing -----------------------------------------------------------

    def draw(self) -> None:
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        body = height - 2  # one line for the clock bar, one for the status line

        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + body:
            self.top = self.cursor - body + 1

        for screen_y in range(body):
            idx = self.top + screen_y
            if idx >= len(self.rows):
                break
            row = self.rows[idx]
            attr = self._row_attr(row)
            if idx == self.cursor:
                attr |= curses.A_REVERSE
            text = row.text[: width - 1]
            if idx == self.cursor:
                text = text.ljust(width - 1)
            try:
                self.stdscr.addstr(screen_y, 0, text, attr)
            except curses.error:
                pass

        self._draw_clockbar(height - 2, width)
        self._draw_status(height - 1, width)
        self.stdscr.refresh()

    def _row_attr(self, row) -> int:
        if row.warn:
            return self.color(CP_WARN, bold=True)
        if row.kind == PROJECT:
            return self.color(CP_PROJECT, bold=True)
        if row.kind == TASK:
            return self.color(CP_RUNNING, bold=True) if row.running \
                else self.color(CP_STATUS)
        if row.kind == COMMENT:
            return self.color(CP_COMMENT)
        return curses.A_NORMAL

    def _draw_clockbar(self, y: int, width: int) -> None:
        active = self.doc.running()
        if active:
            from .model import human_duration
            project, task, clock = active
            text = (f" RUNNING {human_duration(clock.duration())} on "
                    f"{task.name} ({project.name}, since "
                    f"{clock.start.strftime('%H:%M')}) ")
        else:
            text = " no clock running "
        try:
            self.stdscr.addstr(y, 0, text.ljust(width - 1)[: width - 1],
                               self.color(CP_BAR))
        except curses.error:
            pass

    def _draw_status(self, y: int, width: int) -> None:
        msg = self.message or "?: help   q: quit   N/n: new   e: edit   d: del   i/o: clock"
        try:
            self.stdscr.addstr(y, 0, msg[: width - 1])
        except curses.error:
            pass

    # -- undo --------------------------------------------------------------

    def checkpoint(self) -> None:
        self._undo.append(copy.deepcopy(self.doc))
        del self._undo[:-UNDO_LIMIT]
        self._redo.clear()

    def action_undo(self) -> None:
        if not self._undo:
            self.message = "Nothing to undo"
            return
        self._redo.append(self.doc)
        self.doc = self._undo.pop()
        self.save_and_refresh()
        self.message = f"Undone ({len(self._undo)} more, Ctrl+R to redo)"

    def action_redo(self) -> None:
        if not self._redo:
            self.message = "Nothing to redo"
            return
        self._undo.append(self.doc)
        self.doc = self._redo.pop()
        self.save_and_refresh()
        self.message = "Redone"

    # -- key dispatch ------------------------------------------------------

    def handle_key(self, ch) -> None:
        self.message = ""
        nav = {
            curses.KEY_UP: -1, curses.KEY_DOWN: 1,
            "k": -1, "j": 1,
        }
        if ch in nav:
            self.move(nav[ch])
        elif ch == curses.KEY_NPAGE:
            self.move(10)
        elif ch == curses.KEY_PPAGE:
            self.move(-10)
        elif ch in (curses.KEY_HOME, "g"):
            self.cursor = 0
        elif ch in (curses.KEY_END, "G"):
            self.cursor = len(self.rows) - 1
        elif ch == "J":
            self.jump_to_running()
        elif ch == "C":
            self.collapse_all()
        elif ch in ("\n", "\r", curses.KEY_ENTER, " ", "\t"):
            self.toggle_collapse()
        elif ch == "N":
            self.new_project()
        elif ch == "n":
            self.new_task()
        elif ch == "e":
            self.edit()
        elif ch == "d":
            self.delete()
        elif ch == "m":
            self.comment()
        elif ch == "X":
            self.expunge()
        elif ch == "i":
            self.clock_in()
        elif ch == "o":
            self.clock_out()
        elif ch == "I":
            self.clock_in_at()
        elif ch == "O":
            self.clock_out_at()
        elif ch == "t":
            self.cycle_status()
        elif isinstance(ch, str) and ch in "12345":
            self.set_priority(int(ch))
        elif ch == "u":
            self.action_undo()
        elif ch in ("\x12", 18):  # Ctrl+R (str from get_wch, int from getch)
            self.action_redo()
        elif ch == "c":
            self.check()
        elif ch == "R":
            self.report()
        elif ch == "r":
            self.reload()
        elif ch == "?":
            self.show_report("Help", HELP_LINES, plain=True)
        elif ch in ("q", "Q"):
            self._running = False
        elif ch == curses.KEY_RESIZE:
            pass

    def move(self, delta: int) -> None:
        if self.rows:
            self.cursor = max(0, min(self.cursor + delta, len(self.rows) - 1))

    def toggle_collapse(self) -> None:
        obj = self.selected_item()
        if isinstance(obj, (Project, Task)):
            obj.collapsed = not obj.collapsed
            self.refresh_rows()

    def _select_obj(self, obj) -> bool:
        for i, row in enumerate(self.rows):
            if row.obj is obj:
                self.cursor = i
                return True
        return False

    def _project_of_selection(self):
        obj = self.selected_item()
        if isinstance(obj, Project):
            return obj
        if isinstance(obj, Task):
            return self.doc.project_of(obj)
        if isinstance(obj, ClockEntry):
            task = self.doc.task_of(obj)
            return self.doc.project_of(task) if task else None
        return None

    def jump_to_running(self) -> None:
        active = self.doc.running()
        if active is None:
            self.message = "No clock is running"
            return
        _, task, _ = active
        self.doc.project_of(task).collapsed = False
        self.refresh_rows()
        self._select_obj(task)
        self.message = f"Jumped to running task: {task.name}"

    def collapse_all(self) -> None:
        project = self._project_of_selection()
        for p in self.doc.projects:
            p.collapsed = True
        self.refresh_rows()
        if project is not None:
            self._select_obj(project)
        self.message = "Collapsed all projects"

    # -- actions: items ----------------------------------------------------

    def new_project(self) -> None:
        result = self.item_form("New project")
        if result:
            self.checkpoint()
            self.doc.projects.append(Project(**result))
            self.save_and_refresh()

    def new_task(self) -> None:
        obj = self.selected_item()
        project = obj if isinstance(obj, Project) else (
            self.doc.project_of(t) if (t := self.selected_task()) else None)
        if project is None:
            self.message = "Select a project first"
            return
        result = self.item_form(f"New task in {project.name}", status="TODO")
        if result:
            self.checkpoint()
            project.tasks.append(Task(**result))
            project.collapsed = False
            self.save_and_refresh()

    def edit(self) -> None:
        obj = self.selected_obj()
        if isinstance(obj, CommentRef):
            self.edit_comment(obj.owner)
        elif isinstance(obj, ClockEntry):
            self.edit_clock(obj)
        elif isinstance(obj, (Project, Task)):
            is_task = isinstance(obj, Task)
            result = self.item_form(
                f"Edit {'task' if is_task else 'project'}",
                name=obj.name, description=obj.description, priority=obj.priority,
                status=obj.status if is_task else None,
            )
            if result:
                self.checkpoint()
                for key, value in result.items():
                    setattr(obj, key, value)
                self.save_and_refresh()
        else:
            self.message = "Nothing to edit"

    def edit_clock(self, clock: ClockEntry) -> None:
        start = self.prompt("Start time", clock.start.strftime("%Y-%m-%d %H:%M"))
        if start is None:
            return
        start_dt = parse_user_ts(start)
        if start_dt is None:
            self.message = "Invalid start time"
            return
        end_default = clock.end.strftime("%Y-%m-%d %H:%M") if clock.end else ""
        end = self.prompt("End time (blank = running)", end_default)
        if end is None:
            return
        end_dt = None
        if end.strip():
            end_dt = parse_user_ts(end, base=start_dt)
            if end_dt is None:
                self.message = "Invalid end time"
                return
            if end_dt < start_dt:
                self.message = "End is before start"
                return
        self.checkpoint()
        clock.start, clock.end = start_dt, end_dt
        self.save_and_refresh()
        self.warn_about(clock)

    def delete(self) -> None:
        obj = self.selected_obj()
        if obj is None:
            self.message = "Nothing selected"
            return
        if isinstance(obj, Project):
            msg = f"Delete project '{obj.name}' and its {len(obj.tasks)} task(s)?"
        elif isinstance(obj, Task):
            msg = f"Delete task '{obj.name}'?"
        elif isinstance(obj, CommentRef):
            msg = "Delete this comment block?"
        else:
            msg = "Delete this clock entry (and its comments)?"
        if not self.confirm(msg):
            return
        self.checkpoint()
        if isinstance(obj, Project):
            self.doc.tombstones.extend(tombstoned(obj.lines()))
            self.doc.projects.remove(obj)
        elif isinstance(obj, Task):
            project = self.doc.project_of(obj)
            project.tombstones.extend(tombstoned(obj.lines()))
            project.tasks.remove(obj)
        elif isinstance(obj, CommentRef):
            owner = obj.owner
            owner.tombstones.extend(tombstoned(comment_lines(owner.comments)))
            owner.comments.clear()
        else:
            task = self.doc.task_of(obj)
            task.tombstones.extend(tombstoned(obj.lines()))
            task.clocks.remove(obj)
        self.save_and_refresh()
        self.message = "Deleted (kept as ## lines — press X to expunge)"

    def expunge(self) -> None:
        count = self.doc.tombstone_count()
        if count == 0:
            self.message = "No deleted lines to expunge"
            return
        if not self.confirm(f"Permanently remove {count} deleted (##) line(s)?"):
            return
        self.checkpoint()
        removed = self.doc.expunge()
        self.save_and_refresh()
        self.message = f"Expunged {removed} deleted line(s)"

    # -- actions: comments -------------------------------------------------

    def comment(self) -> None:
        owner = self.selected_item()
        if owner is None:
            self.message = "Select a line to comment on"
            return
        self.edit_comment(owner)

    def edit_comment(self, owner) -> None:
        text = self.edit_multiline("Comment (Ctrl+G save, Esc cancel)",
                                   "\n".join(owner.comments))
        if text is None:
            return
        lines = [ln.rstrip() for ln in text.splitlines()]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        self.checkpoint()
        owner.comments = lines
        if isinstance(owner, (Project, Task)):
            owner.collapsed = False
        if isinstance(owner, Task):
            self.doc.project_of(owner).collapsed = False
        elif isinstance(owner, ClockEntry):
            task = self.doc.task_of(owner)
            task.collapsed = False
            self.doc.project_of(task).collapsed = False
        self.save_and_refresh()

    # -- actions: clocking -------------------------------------------------

    def clock_in(self) -> None:
        task = self.selected_task()
        if task is None:
            self.message = "Select a task to clock in"
            return
        self.checkpoint()
        self.doc.clock_in(task)
        task.collapsed = False
        self.save_and_refresh()
        self.message = f"Clocked in: {task.name}"

    def clock_out(self) -> None:
        active = self.doc.running()
        if active is None:
            self.message = "No clock is running"
            return
        _, task, clock = active
        self.checkpoint()
        self.doc.clock_out()
        self.save_and_refresh()
        self.message = f"Clocked out: {task.name}"
        self.warn_about(clock)

    def clock_in_at(self) -> None:
        task = self.selected_task()
        if task is None:
            self.message = "Select a task to clock in"
            return
        when = self.prompt_time("Clock in at")
        if when is None:
            return
        self.checkpoint()
        self.doc.clock_in(task, when)
        task.collapsed = False
        self.save_and_refresh()
        self.message = f"Clocked in: {task.name} at {when:%H:%M}"
        self.warn_about(task.clocks[-1])

    def clock_out_at(self) -> None:
        active = self.doc.running()
        if active is None:
            self.message = "No clock is running"
            return
        _, task, clock = active
        when = self.prompt_time("Clock out at", base=clock.start)
        if when is None:
            return
        if when < clock.start:
            self.message = f"End is before the clock start ({format_ts(clock.start)})"
            return
        self.checkpoint()
        clock.end = when
        self.save_and_refresh()
        self.message = f"Clocked out: {task.name} at {when:%H:%M}"
        self.warn_about(clock)

    def cycle_status(self) -> None:
        task = self.selected_task()
        if task is None:
            self.message = "Select a task to change status"
            return
        self.checkpoint()
        task.status = STATUSES[(STATUSES.index(task.status) + 1) % len(STATUSES)]
        self.save_and_refresh()

    def set_priority(self, value: int) -> None:
        obj = self.selected_item()
        if isinstance(obj, ClockEntry):
            obj = self.doc.task_of(obj)
        if not isinstance(obj, (Project, Task)):
            self.message = "Select a project or task"
            return
        self.checkpoint()
        obj.priority = value
        self.save_and_refresh()

    # -- actions: misc -----------------------------------------------------

    def check(self) -> None:
        problems = self.load_issues + check_consistency(self.doc)
        self.show_report("Consistency check", problems or ["No problems found."],
                         plain=True)

    def prompt_date(self, label: str):
        """Prompt for an optional date.  Returns a date, None (blank = open),
        or the _CANCEL sentinel if Esc was pressed."""
        default = ""
        while True:
            raw = self.prompt(f"{label} (YYYY-MM-DD, blank = open-ended)", default)
            if raw is None:
                return _CANCEL
            raw = raw.strip()
            if not raw:
                return None
            d = parse_user_date(raw)
            if d is not None:
                return d
            self.message = "Invalid date; try again"
            default = raw

    def report(self) -> None:
        start = self.prompt_date("Report start")
        if start is _CANCEL:
            return
        end = self.prompt_date("Report end")
        if end is _CANCEL:
            return
        if start is not None and end is not None and end < start:
            self.message = "End date is before start date"
            return
        default = default_filename(self.doc, start, end)
        name = self.prompt("Write report to file", default)
        if name is None:
            return
        name = name.strip() or default
        out_path = Path(name)
        if not out_path.is_absolute():
            base = self.doc.path.parent if self.doc.path else Path.cwd()
            out_path = base / out_path
        try:
            out_path.write_text(build_report(self.doc, start, end),
                                encoding="utf-8")
        except OSError as exc:
            self.message = f"Could not write report: {exc}"
            return
        self.message = f"Wrote report to {out_path}"

    def reload(self) -> None:
        self.checkpoint()
        self.doc, self.load_issues = load(self.doc.path)
        self.refresh_rows()
        self.message = f"Reloaded {self.doc.path}"
        if self.load_issues:
            self.message += f" ({len(self.load_issues)} problem(s); press c)"

    def warn_about(self, clock: ClockEntry) -> None:
        warnings = clock_warnings(clock)
        if warnings:
            self.message = "WARNING: clock " + "; ".join(warnings)

    # -- dialogs -----------------------------------------------------------

    def _centered_win(self, height: int, width: int):
        maxy, maxx = self.stdscr.getmaxyx()
        height = min(height, maxy - 2)
        width = min(width, maxx - 2)
        y = max(0, (maxy - height) // 2)
        x = max(0, (maxx - width) // 2)
        win = curses.newwin(height, width, y, x)
        win.keypad(True)
        win.box()
        return win

    def prompt(self, label: str, initial: str = "") -> str | None:
        """Single-line text input.  Returns the string, or None on Esc."""
        win = self._centered_win(5, max(40, len(label) + 6))
        h, w = win.getmaxyx()
        win.addstr(1, 2, label[: w - 4])
        buf = list(initial)
        pos = len(buf)
        curses.curs_set(1)
        try:
            while True:
                field = "".join(buf)
                win.addstr(2, 2, " " * (w - 4))
                win.addstr(2, 2, field[: w - 4])
                win.move(2, 2 + min(pos, w - 5))
                win.refresh()
                ch = win.get_wch()
                if ch == "\x1b":            # Esc
                    return None
                if ch in ("\n", "\r", curses.KEY_ENTER):
                    return "".join(buf)
                if ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    if pos > 0:
                        del buf[pos - 1]
                        pos -= 1
                elif ch == curses.KEY_DC:
                    if pos < len(buf):
                        del buf[pos]
                elif ch == curses.KEY_LEFT:
                    pos = max(0, pos - 1)
                elif ch == curses.KEY_RIGHT:
                    pos = min(len(buf), pos + 1)
                elif ch == curses.KEY_HOME:
                    pos = 0
                elif ch == curses.KEY_END:
                    pos = len(buf)
                elif ch == "\x15":          # Ctrl+U clear
                    buf, pos = [], 0
                elif isinstance(ch, str) and ch.isprintable():
                    buf.insert(pos, ch)
                    pos += 1
        finally:
            curses.curs_set(0)

    def prompt_time(self, label: str, base: datetime | None = None) -> datetime | None:
        default = datetime.now().strftime("%Y-%m-%d %H:%M")
        while True:
            raw = self.prompt(f"{label} (YYYY-MM-DD HH:MM or HH:MM)", default)
            if raw is None:
                return None
            when = parse_user_ts(raw, base)
            if when is not None:
                return when
            self.message = "Invalid time; try again"
            default = raw

    def prompt_choice(self, label: str, options: list[str], index: int) -> int | None:
        """Left/Right (or number) to pick; Enter accepts; Esc cancels."""
        win = self._centered_win(5, max(40, len(label) + 6, sum(len(o) for o in options) + 10))
        h, w = win.getmaxyx()
        win.addstr(1, 2, label[: w - 4])
        win.addstr(3, 2, "<-/-> choose, Enter ok, Esc cancel"[: w - 4])
        while True:
            x = 2
            win.move(2, 2)
            win.addstr(2, 2, " " * (w - 4))
            x = 2
            for i, opt in enumerate(options):
                attr = curses.A_REVERSE if i == index else curses.A_NORMAL
                if x + len(opt) < w - 2:
                    win.addstr(2, x, opt, attr)
                    x += len(opt) + 2
            win.refresh()
            ch = win.get_wch()
            if ch == "\x1b":
                return None
            if ch in ("\n", "\r", curses.KEY_ENTER):
                return index
            if ch == curses.KEY_LEFT:
                index = (index - 1) % len(options)
            elif ch == curses.KEY_RIGHT:
                index = (index + 1) % len(options)
            elif isinstance(ch, str) and ch.isdigit():
                d = int(ch)
                if 1 <= d <= len(options):
                    index = d - 1

    def item_form(self, title: str, *, name: str = "", description: str = "",
                  priority: int = 3, status: str | None = None) -> dict | None:
        """Collect fields for a project/task via a sequence of prompts."""
        self.message = title
        new_name = self.prompt("Name", name)
        if new_name is None or not new_name.strip():
            if new_name is not None:
                self.message = "Name must not be empty — cancelled"
            return None
        new_desc = self.edit_multiline(
            "Description (Ctrl+G save, Esc skip)", description)
        if new_desc is None:
            new_desc = description
        else:
            new_desc = new_desc.strip()
        pri_idx = self.prompt_choice("Priority (1 = highest)",
                                     [str(p) for p in range(1, 6)], priority - 1)
        if pri_idx is None:
            return None
        result = {"name": new_name.strip(), "description": new_desc,
                  "priority": pri_idx + 1}
        if status is not None:
            st_idx = self.prompt_choice("Status", STATUSES, STATUSES.index(status))
            if st_idx is None:
                return None
            result["status"] = STATUSES[st_idx]
        return result

    def edit_multiline(self, title: str, initial: str = "") -> str | None:
        """Full multi-line text editor.

        Maintains a list of lines and a (row, col) cursor so that Enter
        splits the current line at the cursor (inserting a blank line in the
        middle works), Backspace joins with the previous line at column 0,
        etc.  Ctrl+G saves, Esc cancels (returns None).

        This replaces curses.textpad.Textbox, which is a fixed character
        grid and cannot insert a newline in the middle of existing text.
        """
        maxy, maxx = self.stdscr.getmaxyx()
        height = min(maxy - 4, 16)
        width = min(maxx - 4, 74)
        y = max(0, (maxy - height) // 2)
        x = max(0, (maxx - width) // 2)
        win = curses.newwin(height, width, y, x)
        win.keypad(True)

        inner_h = height - 2          # text rows inside the box
        inner_w = width - 2           # text cols inside the box
        lines = initial.split("\n") or [""]
        if not lines:
            lines = [""]
        cy = len(lines) - 1           # start at end of existing text
        cx = len(lines[cy])
        top = 0                       # first visible line (vertical scroll)

        def is_enter(ch):
            return ch in ("\n", "\r", curses.KEY_ENTER, 10, 13)

        def is_backspace(ch):
            return ch in (curses.KEY_BACKSPACE, "\x7f", "\b", "\x08", 127, 8)

        def is_save(ch):
            return ch in ("\x07", 7)        # Ctrl+G

        def is_cancel(ch):
            return ch in ("\x1b", 27)       # Esc

        curses.curs_set(1)
        try:
            while True:
                # keep cursor visible (vertical) and compute horizontal scroll
                top = max(min(top, cy), cy - inner_h + 1, 0)
                left = 0
                if cx >= inner_w:
                    left = cx - inner_w + 1

                win.erase()
                win.box()
                win.addstr(0, 2, f" {title[: width - 6]} ")
                hint = "Enter: newline  Ctrl+G: save  Esc: cancel"
                try:
                    win.addstr(height - 1, 2, hint[: width - 4])
                except curses.error:
                    pass
                for i in range(inner_h):
                    li = top + i
                    if li >= len(lines):
                        break
                    seg = lines[li][left: left + inner_w]
                    try:
                        win.addstr(1 + i, 1, seg)
                    except curses.error:
                        pass
                win.move(1 + (cy - top), 1 + (cx - left))
                win.refresh()

                ch = win.get_wch()
                if is_cancel(ch):
                    return None
                if is_save(ch):
                    return "\n".join(ln.rstrip() for ln in lines)
                if is_enter(ch):
                    rest = lines[cy][cx:]
                    lines[cy] = lines[cy][:cx]
                    lines.insert(cy + 1, rest)
                    cy, cx = cy + 1, 0
                elif is_backspace(ch):
                    if cx > 0:
                        lines[cy] = lines[cy][:cx - 1] + lines[cy][cx:]
                        cx -= 1
                    elif cy > 0:
                        cx = len(lines[cy - 1])
                        lines[cy - 1] += lines[cy]
                        del lines[cy]
                        cy -= 1
                elif ch == curses.KEY_DC:
                    if cx < len(lines[cy]):
                        lines[cy] = lines[cy][:cx] + lines[cy][cx + 1:]
                    elif cy < len(lines) - 1:
                        lines[cy] += lines[cy + 1]
                        del lines[cy + 1]
                elif ch == curses.KEY_LEFT:
                    if cx > 0:
                        cx -= 1
                    elif cy > 0:
                        cy -= 1
                        cx = len(lines[cy])
                elif ch == curses.KEY_RIGHT:
                    if cx < len(lines[cy]):
                        cx += 1
                    elif cy < len(lines) - 1:
                        cy += 1
                        cx = 0
                elif ch == curses.KEY_UP:
                    if cy > 0:
                        cy -= 1
                        cx = min(cx, len(lines[cy]))
                elif ch == curses.KEY_DOWN:
                    if cy < len(lines) - 1:
                        cy += 1
                        cx = min(cx, len(lines[cy]))
                elif ch == curses.KEY_HOME:
                    cx = 0
                elif ch == curses.KEY_END:
                    cx = len(lines[cy])
                elif ch == "\t":
                    lines[cy] = lines[cy][:cx] + "    " + lines[cy][cx:]
                    cx += 4
                elif isinstance(ch, str) and ch.isprintable():
                    lines[cy] = lines[cy][:cx] + ch + lines[cy][cx:]
                    cx += 1
        finally:
            curses.curs_set(0)

    def confirm(self, message: str) -> bool:
        win = self._centered_win(5, max(40, len(message) + 6))
        h, w = win.getmaxyx()
        win.addstr(1, 2, message[: w - 4])
        win.addstr(3, 2, "y = yes, any other key = no"[: w - 4])
        win.refresh()
        ch = win.get_wch()
        return ch in ("y", "Y")

    def show_report(self, title: str, lines, plain: bool = False) -> None:
        maxy, maxx = self.stdscr.getmaxyx()
        height = min(maxy - 2, len(lines) + 4)
        width = min(maxx - 2, max([len(title)] + [len(s) for s in lines]) + 6)
        width = max(width, 30)
        win = self._centered_win(height, width)
        h, w = win.getmaxyx()
        win.addstr(0, 2, f" {title[: w - 6]} ")
        offset = 0
        body = h - 3
        while True:
            for i in range(body):
                win.addstr(i + 1, 2, " " * (w - 4))
                if offset + i < len(lines):
                    line = lines[offset + i]
                    prefix = "" if plain else "- "
                    win.addstr(i + 1, 2, (prefix + str(line))[: w - 4])
            footer = "Up/Down scroll, any other key close" if len(lines) > body \
                else "press any key to close"
            win.addstr(h - 1, 2, footer[: w - 4])
            win.refresh()
            ch = win.get_wch()
            if ch == curses.KEY_DOWN and offset + body < len(lines):
                offset += 1
            elif ch == curses.KEY_UP and offset > 0:
                offset -= 1
            else:
                break


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("timelog.org")
    path = path.resolve()
    curses.wrapper(lambda stdscr: CursesApp(stdscr, path).run())


if __name__ == "__main__":
    main()
