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
from datetime import date, datetime, timedelta
from pathlib import Path

from .model import (
    CLOSED_STATUSES,
    STATUSES,
    ClockEntry,
    Document,
    Project,
    Task,
    apply_overlap_changes,
    apply_reshape,
    apply_snap_fix,
    check_consistency,
    clock_warnings,
    comment_lines,
    describe_change,
    describe_snap_fix,
    find_deleted_items,
    find_snap_fixes,
    format_ts,
    load,
    make_backup,
    overlap_changes,
    parse_user_date,
    parse_user_ts,
    reshape_to_avoid,
    soft_delete_lines,
)
from .calimport import parse_csv, plan_import
from .config import Config, load_config, save_config
from .report import build_report, default_filename
from .view import (
    COMMENT,
    ENTRY,
    GAP,
    PROJECT,
    SORT_MODES,
    TASK,
    CommentRef,
    HELP_LINES,
    describe_deleted_item,
    flatten,
    next_match_index,
    priority_rows,
    search_targets,
    timeline_hidden_counts,
    timeline_rows,
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
CP_ANCIENT = 7
CP_STALE = CP_COMMENT  # reuse the green pair, dimmed, for "not touched in a while"


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
        self.search_term = ""
        self.sort_mode = "file"
        self.mode = "normal"  # "normal" | "priority" (see priority_mode())
        self.config = load_config(path)

    # -- setup -------------------------------------------------------------

    def run(self) -> None:
        curses.curs_set(0)
        self.stdscr.keypad(True)
        self._init_colors()
        make_backup(self.doc.path)  # snapshot the file as it was at launch
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
        curses.init_pair(CP_ANCIENT, curses.COLOR_WHITE, -1)

    def color(self, pair: int, bold: bool = False, dim: bool = False) -> int:
        if not self.has_color:
            attr = curses.A_BOLD if bold else curses.A_NORMAL
            return attr | curses.A_DIM if dim else attr
        attr = curses.color_pair(pair)
        if bold:
            attr |= curses.A_BOLD
        if dim:
            attr |= curses.A_DIM
        return attr

    # -- model/view sync ---------------------------------------------------

    def refresh_rows(self) -> None:
        """Rebuild the visible-row list, keeping the cursor on the same item."""
        prev = self.selected_obj()
        prev_owner = prev.owner if isinstance(prev, CommentRef) else None
        if self.mode == "priority":
            self.rows = priority_rows(self.doc, self._now())
        else:
            self.rows = flatten(self.doc, sort_mode=self.sort_mode)
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
            if row.stale == "ancient":
                return self.color(CP_ANCIENT, bold=True, dim=True)
            if row.stale == "stale":
                return self.color(CP_STALE, bold=True, dim=True)
            return self.color(CP_PROJECT, bold=True)
        if row.kind == TASK:
            if row.running:
                return self.color(CP_RUNNING, bold=True)
            if row.stale == "ancient":
                return self.color(CP_ANCIENT, dim=True)
            if row.stale == "stale":
                return self.color(CP_STALE, dim=True)
            return self.color(CP_STATUS)
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
        if self.message:
            msg = self.message
        elif self.mode == "priority":
            msg = ("PRIORITY VIEW  i: clock in & return   s/S: status   "
                  "1-5: priority   D: done   q: back")
        else:
            msg = "?: help   q: quit   N/n: new   e: edit   d: del   i/o: clock"
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
        elif ch == "z":
            self.cycle_sort()
        elif ch == "A":
            self.import_calendar()
        elif ch == "t":
            self.timeline_mode()
        elif ch == "p":
            self.priority_mode()
        elif ch == "/":
            self.search()
        elif ch == "m":
            self.move_task()
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
        elif ch == "c":
            self.comment()
        elif ch == "X":
            self.expunge()
        elif ch == "R":
            self.restore_deleted()
        elif ch == "H":
            self.snap_overlaps()
        elif ch == "i":
            self.clock_in()
        elif ch == "o":
            self.clock_out()
        elif ch == "I":
            self.clock_in_at()
        elif ch == "O":
            self.clock_out_at()
        elif ch == "s":
            self.cycle_status(1)
        elif ch == "S":
            self.cycle_status(-1)
        elif ch == "D":
            self.mark_done()
        elif isinstance(ch, str) and ch in "12345":
            self.set_priority(int(ch))
        elif ch == "u":
            self.action_undo()
        elif ch == "U":
            self.action_redo()
        elif ch == "v":
            self.check()
        elif ch == "r":
            self.report()
        elif ch == "L":
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
        project, task, clock = active
        project.collapsed = False
        task.collapsed = False
        self.refresh_rows()
        self._select_obj(clock)
        self.message = f"Jumped to running clock on {task.name}"

    def collapse_all(self) -> None:
        project = self._project_of_selection()
        for p in self.doc.projects:
            p.collapsed = True
        self.refresh_rows()
        if project is not None:
            self._select_obj(project)
        self.message = "Collapsed all projects"

    def cycle_sort(self) -> None:
        self.sort_mode = SORT_MODES[
            (SORT_MODES.index(self.sort_mode) + 1) % len(SORT_MODES)]
        self.refresh_rows()
        labels = {"file": "file order", "priority": "priority (1 first)",
                  "created": "created (oldest first)",
                  "modified": "modified (newest first)"}
        self.message = f"Sort: {labels[self.sort_mode]}"

    # -- search ------------------------------------------------------------

    def search(self) -> None:
        term = self.prompt("Search (substring)", self.search_term)
        if term is None:
            return
        term = term.strip()
        if not term:
            return
        self.search_term = term
        self._do_search()

    def _current_target_index(self, targets) -> int:
        obj = self.selected_obj()
        owner = obj.owner if isinstance(obj, CommentRef) else obj
        for i, target in enumerate(targets):
            if target.owner is owner:
                return i
        return -1

    def _do_search(self) -> None:
        targets = search_targets(self.doc)
        if not targets:
            self.message = "Nothing to search"
            return
        idx = next_match_index(targets, self.search_term,
                               self._current_target_index(targets))
        if idx is None:
            self.message = f"No match for {self.search_term!r}"
            return
        self._reveal_and_select(targets[idx])
        self.message = f"Search: {self.search_term}  (/ to repeat)"

    def _reveal_and_select(self, target) -> None:
        target.project.collapsed = False
        if target.kind == COMMENT and target.task is not None:
            target.task.collapsed = False
        self.refresh_rows()
        if target.kind == COMMENT:
            for i, row in enumerate(self.rows):
                if row.kind == COMMENT and row.obj.owner is target.owner:
                    self.cursor = i
                    return
        else:
            self._select_obj(target.owner)

    # -- move a task to another project ------------------------------------

    def move_task(self) -> None:
        task = self.selected_task()
        if task is None:
            self.message = "Select a task to move"
            return
        src = self.doc.project_of(task)
        others = [p for p in self.doc.projects if p is not src]
        if not others:
            self.message = "No other project to move to"
            return
        idx = self.prompt_list_choice(f"Move '{task.name}' to project",
                                      [p.name for p in others], 0)
        if idx is None:
            return
        dest = others[idx]
        self.checkpoint()
        src.tasks.remove(task)
        dest.tasks.append(task)
        dest.collapsed = False
        now = self._now()
        task.modified = src.modified = dest.modified = now
        self.save_and_refresh()
        self._select_obj(task)
        self.message = f"Moved '{task.name}' to {dest.name}"

    # -- actions: items ----------------------------------------------------

    def _now(self) -> datetime:
        return datetime.now().replace(second=0, microsecond=0)

    def new_project(self) -> None:
        name = self.prompt("New project name")
        if name is None or not name.strip():
            return
        self.checkpoint()
        now = self._now()
        project = Project(name=name.strip(), created=now, modified=now)
        self.doc.projects.append(project)
        self.save_and_refresh()
        self._select_obj(project)
        self.message = "Created project (1-5 sets priority)"

    def new_task(self) -> None:
        obj = self.selected_item()
        project = obj if isinstance(obj, Project) else (
            self.doc.project_of(t) if (t := self.selected_task()) else None)
        if project is None:
            self.message = "Select a project first"
            return
        name = self.prompt(f"New task in {project.name}")
        if name is None or not name.strip():
            return
        self.checkpoint()
        now = self._now()
        task = Task(name=name.strip(), created=now, modified=now)
        project.tasks.append(task)
        project.modified = now           # a new task modifies its project
        project.collapsed = False
        self.save_and_refresh()
        self._select_obj(task)
        self.message = "Created task (1-5 priority, s/S/D status)"

    def edit(self) -> None:
        obj = self.selected_obj()
        if isinstance(obj, CommentRef):
            self.edit_comment(obj.owner)
        elif isinstance(obj, ClockEntry):
            self.edit_clock(obj)
        elif isinstance(obj, (Project, Task)):
            kind = "task" if isinstance(obj, Task) else "project"
            name = self.prompt(f"Rename {kind}", obj.name)
            if name is None or not name.strip():
                return
            # optionally fix the created time (e.g. project created late)
            created = self.prompt(
                "Created (YYYY-MM-DD HH:MM, blank = keep)",
                obj.created.strftime("%Y-%m-%d %H:%M") if obj.created else "")
            if created is None:
                return
            self.checkpoint()
            obj.name = name.strip()
            if created.strip():
                new_created = parse_user_ts(created)
                if new_created is None:
                    self.message = "Invalid created time; name updated, date kept"
                else:
                    obj.created = new_created
            self.doc.touch(obj)
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
        # detect/resolve overlaps with other entries (only for closed edits)
        changes = []
        if end_dt is not None:
            changes = overlap_changes(self.doc, clock, start_dt, end_dt)
        if not changes:
            self.checkpoint()
            clock.start, clock.end = start_dt, end_dt
            self.doc.touch(clock)
            self.save_and_refresh()
            self.warn_about(clock)
            return

        # overlap found: let the user choose which time takes precedence
        pieces = reshape_to_avoid(self.doc, clock, start_dt, end_dt)

        def fmt(s, e):
            return f"{s:%Y-%m-%d %H:%M}--{e:%Y-%m-%d %H:%M}"

        n = len(changes)
        lines = [f"This time overlaps {n} other entr{'y' if n == 1 else 'ies'}."
                 "  Whose time takes precedence?", ""]
        lines.append("[e] This EDITED time wins — the others are adjusted:")
        lines += ["      " + describe_change(c) for c in changes]
        lines += ["", "[o] The OTHER entries win — this edited time is adjusted:"]
        if not pieces:
            lines.append("      this time -> 0:00 — WIPED OUT (possible typo)")
        else:
            lines.append("      this time -> " + "  +  ".join(
                fmt(s, e) for s, e in pieces))
        wipe_notes = []
        if any(c.becomes_zero for c in changes):
            wipe_notes.append("[e] wipes out another entry")
        if not pieces:
            wipe_notes.append("[o] wipes out this edit")
        if wipe_notes:
            lines += ["", "Note: " + "; ".join(wipe_notes)
                      + " (0:00) — likely a typo."]
        lines += ["", "e = edited wins    o = others win    Esc = cancel"]

        choice = self.choose_precedence("Resolve overlap", lines)
        if choice is None:
            self.message = "Edit cancelled (overlap not resolved)"
            return
        self.checkpoint()
        if choice == "e":
            clock.start, clock.end = start_dt, end_dt
            apply_overlap_changes(changes)
            for c in changes:
                self.doc.touch(c.clock)
            self.doc.touch(clock)
            self.message = f"Edited time wins; adjusted {n} other entry(s)"
        else:  # others win: reshape the edited entry around them
            apply_reshape(self.doc, clock, pieces, start_dt)
            self.doc.touch(clock)
            self.message = ("Others win; this time wiped to 0:00"
                            if not pieces else
                            f"Others win; this time reshaped into "
                            f"{len(pieces)} piece(s)")
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
        now = self._now()
        if isinstance(obj, Project):
            self.doc.tombstones.extend(soft_delete_lines(obj.lines(), now))
            self.doc.projects.remove(obj)
        elif isinstance(obj, Task):
            project = self.doc.project_of(obj)
            project.tombstones.extend(soft_delete_lines(obj.lines(), now))
            project.tasks.remove(obj)
            self.doc.touch(project)
        elif isinstance(obj, CommentRef):
            owner = obj.owner
            owner.tombstones.extend(
                soft_delete_lines(comment_lines(owner.comments), now))
            owner.comments.clear()
            self.doc.touch(owner)
        else:
            self._remove_clock(self.doc.task_of(obj), obj)
        self.save_and_refresh()
        self.message = "Deleted (kept as ## lines — press X to expunge, R to restore)"

    def _remove_clock(self, task: Task, clock: ClockEntry) -> None:
        """Tombstone one clock entry and drop it from its task.  Caller must
        checkpoint() first and confirm with the user; shared by delete()
        (normal view) and timeline_mode()'s own 'd' key."""
        task.tombstones.extend(soft_delete_lines(clock.lines(), self._now()))
        task.clocks.remove(clock)
        self.doc.touch(task)

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

    def restore_deleted(self) -> None:
        items = find_deleted_items(self.doc, self._now())
        if not items:
            self.message = "No deleted items to restore"
            return
        options = [describe_deleted_item(it) for it in items]
        idx = self.prompt_list_choice("Restore (most recent deletion first)",
                                      options, 0)
        if idx is None:
            self.message = "Restore cancelled"
            return
        item = items[idx]
        self.checkpoint()
        self.doc.restore(item)
        self.doc.touch(item.obj)
        # expand whatever ancestors might be collapsed so the restored item
        # (and, for a task, its own clocks) is actually visible afterward
        if item.kind == "task":
            item.obj.collapsed = False
            item.owner.collapsed = False
        elif item.kind == "clock":
            item.owner.collapsed = False
            project = self.doc.project_of(item.owner)
            if project is not None:
                project.collapsed = False
        self.save_and_refresh()
        self._select_obj(item.obj)
        self.message = f"Restored: {describe_deleted_item(item)}"

    # -- actions: comments -------------------------------------------------

    def comment(self) -> None:
        owner = self.selected_item()
        if owner is None:
            self.message = "Select a line to comment on"
            return
        self.edit_comment(owner)

    def edit_comment(self, owner) -> None:
        text = self.edit_multiline("Comment (Ctrl+O save, Esc cancel)",
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
        self.doc.touch(owner)
        self.save_and_refresh()

    # -- actions: clocking -------------------------------------------------

    def clock_in(self) -> None:
        task = self.selected_task()
        if task is None:
            self.message = "Select a task to clock in"
            return
        self.checkpoint()
        self.doc.clock_in(task)
        self.doc.touch(task)
        task.collapsed = False
        self.save_and_refresh()
        self._select_obj(task.clocks[-1])
        self.message = f"Clocked in: {task.name}"

    def clock_out(self) -> None:
        active = self.doc.running()
        if active is None:
            self.message = "No clock is running"
            return
        _, task, clock = active
        self.checkpoint()
        self.doc.clock_out()
        self.doc.touch(task)
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
        self.doc.touch(task)
        task.collapsed = False
        self.save_and_refresh()
        self._select_obj(task.clocks[-1])
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
        self.doc.touch(task)
        self.save_and_refresh()
        self.message = f"Clocked out: {task.name} at {when:%H:%M}"
        self.warn_about(clock)

    def _set_status(self, task: Task, status: str) -> None:
        """Set a task's status, closing its running clock if now closed."""
        task.status = status
        if status in CLOSED_STATUSES and task.running_clock():
            self.doc.clock_out()

    def cycle_status(self, step: int = 1) -> None:
        task = self.selected_task()
        if task is None:
            self.message = "Select a task to change status"
            return
        self.checkpoint()
        self._set_status(
            task, STATUSES[(STATUSES.index(task.status) + step) % len(STATUSES)])
        self.doc.touch(task)
        self.save_and_refresh()

    def mark_done(self) -> None:
        obj = self.selected_item()
        if isinstance(obj, Project):
            n = len(obj.tasks)
            if n == 0:
                self.message = "Project has no tasks"
                return
            if not self.confirm(f"Mark all {n} task(s) in '{obj.name}' as DONE?"):
                return
            self.checkpoint()
            for task in obj.tasks:
                self._set_status(task, "DONE")
                self.doc.touch(task)
            self.save_and_refresh()
            self.message = f"Marked {n} task(s) DONE in {obj.name}"
            return
        task = self.selected_task()
        if task is None:
            self.message = "Select a task or project to mark DONE"
            return
        self.checkpoint()
        self._set_status(task, "DONE")
        self.doc.touch(task)
        self.save_and_refresh()
        self.message = f"Marked DONE: {task.name}"

    def set_priority(self, value: int) -> None:
        obj = self.selected_item()
        if isinstance(obj, ClockEntry):
            obj = self.doc.task_of(obj)
        if not isinstance(obj, (Project, Task)):
            self.message = "Select a project or task"
            return
        self.checkpoint()
        obj.priority = value
        self.doc.touch(obj)
        self.save_and_refresh()

    # -- actions: misc -----------------------------------------------------

    def check(self) -> None:
        problems = self.load_issues + check_consistency(self.doc)
        fixes = find_snap_fixes(self.doc)
        if fixes:
            problems = problems + [
                f"{len(fixes)} small overlap(s) could be snapped to a "
                f"half-hour — press H to review"]
        self.show_report("Consistency check", problems or ["No problems found."],
                         plain=True)

    def snap_overlaps(self) -> None:
        fixes = find_snap_fixes(self.doc)
        if not fixes:
            self.message = "No small overlaps bordering a half-hour found"
            return
        lines = [f"{len(fixes)} small overlap(s) border or contain a "
                 "half-hour mark:", ""]
        lines += ["  " + describe_snap_fix(f) for f in fixes]
        lines += ["", "Snap all of these to the half-hour?"]
        if not self.confirm_list("Snap overlaps to half-hour", lines):
            self.message = "Snap cancelled"
            return
        self.checkpoint()
        for fix in fixes:
            apply_snap_fix(fix)
            self.doc.touch(fix.clock_a)
            self.doc.touch(fix.clock_b)
        self.save_and_refresh()
        self.message = f"Snapped {len(fixes)} overlap(s) to the half-hour"

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

    # -- timeline mode -----------------------------------------------------

    def timeline_mode(self) -> None:
        """Full-screen view of one day's workday with entries and gaps.

        Navigate rows; on a gap press 'a' (or Enter) to add an entry; on an
        entry press 'e' to edit its time or 'd' to delete it (same as the
        normal view); '['/']' change day, 'g' jumps to a date, '<'/'>' widen
        the window earlier/later (beyond the configured default), 'R' resets
        the window, 'W' saves the current window as the new default, 'u'
        undoes, 'q'/Esc returns.
        """
        day = date.today()
        cursor = 0
        start_hour = self.config.workday_start
        end_hour = self.config.workday_end
        while True:
            rows, ws, we = timeline_rows(self.doc, day, start_hour, end_hour)
            hidden_before, hidden_after = timeline_hidden_counts(
                self.doc, day, ws, we)
            cursor = max(0, min(cursor, len(rows) - 1))
            self._draw_timeline(day, rows, ws, we, cursor,
                               hidden_before, hidden_after)
            ch = self.stdscr.get_wch()

            if ch in (curses.KEY_UP, "k"):
                cursor = max(0, cursor - 1)
            elif ch in (curses.KEY_DOWN, "j"):
                cursor = min(len(rows) - 1, cursor + 1)
            elif ch == curses.KEY_HOME:
                cursor = 0
            elif ch == curses.KEY_END:
                cursor = len(rows) - 1
            elif ch == "[":
                day -= timedelta(days=1)
                cursor = 0
            elif ch == "]":
                day += timedelta(days=1)
                cursor = 0
            elif ch == "g":
                when = self.prompt_date("Go to date")
                if when not in (None, _CANCEL):
                    day = when
                    cursor = 0
            elif ch == "<":
                start_hour = max(0, start_hour - 1)
                cursor = 0
            elif ch == ">":
                end_hour = min(24, end_hour + 1)
                cursor = 0
            elif ch == "R":
                start_hour = self.config.workday_start
                end_hour = self.config.workday_end
                cursor = 0
                self.message = "Window reset to default"
            elif ch == "W":
                self.config = Config(start_hour, end_hour)
                save_config(self.doc.path, self.config)
                self.message = (f"Saved default window: "
                                f"{start_hour:02d}:00-{end_hour:02d}:00")
            elif ch == "u":
                self.action_undo()
            elif ch in ("a", "\n", "\r", curses.KEY_ENTER):
                if 0 <= cursor < len(rows) and rows[cursor].kind == GAP:
                    self._add_in_gap(rows[cursor])
                else:
                    self.message = "Select a gap to add an entry"
            elif ch == "e":
                if 0 <= cursor < len(rows) and rows[cursor].kind == ENTRY:
                    self.edit_clock(rows[cursor].clock)
                else:
                    self.message = "Select a time entry to edit"
            elif ch == "d":
                if 0 <= cursor < len(rows) and rows[cursor].kind == ENTRY:
                    row = rows[cursor]
                    if self.confirm("Delete this clock entry (and its comments)?"):
                        self.checkpoint()
                        self._remove_clock(row.task, row.clock)
                        self.message = ("Deleted (kept as ## lines — press X "
                                        "to expunge, R to restore)")
                else:
                    self.message = "Select a time entry to delete"
            elif ch in ("q", "Q", "\x1b"):
                break
            elif ch == curses.KEY_RESIZE:
                pass
        self.refresh_rows()
        self.message = "Left timeline"

    def priority_mode(self) -> None:
        """Flat, cross-project triage view: every open task, sorted by task
        priority then project priority then most-recently-touched first (see
        ``priority_rows``).

        Navigation and status(s/S)/done(D)/priority(1-5) work directly here
        and re-sort or drop the row immediately, same as in the normal view.
        'i' clocks the selected task in and drops straight back to the
        normal view, cursor on the new clock entry -- exactly as if you'd
        pressed 'i' there yourself. 'q'/Esc returns without clocking in.
        """
        self.mode = "priority"
        self.refresh_rows()
        self.cursor = 0  # always land on the most urgent task
        if not self.rows:
            self.message = "No open tasks to prioritize"
            self.mode = "normal"
            self.refresh_rows()
            return
        while True:
            self.draw()
            ch = self.stdscr.get_wch()
            self.message = ""
            if ch in (curses.KEY_UP, "k"):
                self.move(-1)
            elif ch in (curses.KEY_DOWN, "j"):
                self.move(1)
            elif ch == curses.KEY_NPAGE:
                self.move(10)
            elif ch == curses.KEY_PPAGE:
                self.move(-10)
            elif ch in (curses.KEY_HOME, "g"):
                self.cursor = 0
            elif ch in (curses.KEY_END, "G"):
                self.cursor = len(self.rows) - 1
            elif ch == "s":
                self.cycle_status(1)
            elif ch == "S":
                self.cycle_status(-1)
            elif ch == "D":
                self.mark_done()
            elif isinstance(ch, str) and ch in "12345":
                self.set_priority(int(ch))
            elif ch == "i":
                # capture task/project while self.rows still holds this
                # flat priority list -- switching self.mode below rebuilds
                # self.rows via the normal tree, which would no longer
                # resolve the current cursor position the same way
                task = self.selected_task()
                project = self.doc.project_of(task) if task is not None else None
                self.mode = "normal"
                if project is not None:
                    # the task's project may be collapsed (unlike normal
                    # mode, this flat view lets you land on it anyway) --
                    # expand it BEFORE rebuilding rows, so the task (and,
                    # after clock_in(), its new clock entry) is present to
                    # select in the normal-mode row list below
                    project.collapsed = False
                self.refresh_rows()
                if task is not None:
                    self._select_obj(task)
                    self.clock_in()
                return
            elif ch in ("q", "Q", "\x1b"):
                break
            elif ch == curses.KEY_RESIZE:
                pass
            if not self.rows:
                self.message = "No open tasks remain"
                break
        self.mode = "normal"
        self.refresh_rows()

    def _draw_timeline(self, day, rows, ws, we, cursor,
                       hidden_before: int = 0, hidden_after: int = 0) -> None:
        from .model import human_duration
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        worked = sum((r.duration for r in rows if r.kind == ENTRY), timedelta())
        free = sum((r.duration for r in rows if r.kind == GAP), timedelta())
        header = (f" Timeline — {day:%a %Y-%m-%d}   "
                  f"window {ws:%H:%M}-{we:%H:%M} ")
        summary = (f" worked {human_duration(worked)}   free {human_duration(free)}"
                   f"   ({sum(1 for r in rows if r.kind == GAP)} gap(s))")
        hints = []
        if hidden_before:
            hints.append(f"+{hidden_before} earlier (< to expand)")
        if hidden_after:
            hints.append(f"+{hidden_after} later (> to expand)")
        if hints:
            summary += "   " + ", ".join(hints)
        try:
            self.stdscr.addstr(0, 0, header[: width - 1], self.color(CP_BAR))
            self.stdscr.addstr(1, 0, summary[: width - 1],
                               self.color(CP_WARN, bold=True) if hints else 0)
        except curses.error:
            pass

        body_top = 3
        body = height - body_top - 1
        top = max(0, cursor - body + 1) if cursor >= body else 0
        for i in range(body):
            idx = top + i
            if idx >= len(rows):
                break
            row = rows[idx]
            span = f"{row.start:%H:%M}-{row.end:%H:%M}"
            dur = human_duration(row.duration)
            if row.kind == GAP:
                text = f"  {span}  {'─' * 6} GAP {dur} {'─' * 6}"
                attr = self.color(CP_WARN, bold=True)
            else:
                label = f"{row.task.name} ({row.project.name})" if row.task else ""
                text = f"  {span}  {label}   [{dur}]"
                attr = self.color(CP_STATUS)
            if idx == cursor:
                attr |= curses.A_REVERSE
                text = text.ljust(width - 1)
            try:
                self.stdscr.addstr(body_top + i, 0, text[: width - 1], attr)
            except curses.error:
                pass

        footer = ("a/Enter add   [ ] day   < > widen   R reset   W save default"
                  "   g goto   u undo   q back")
        try:
            self.stdscr.addstr(height - 1, 0, footer[: width - 1], self.color(CP_BAR))
        except curses.error:
            pass
        self.stdscr.refresh()

    def _add_in_gap(self, gap) -> None:
        gs, ge = gap.start, gap.end
        s_raw = self.prompt(f"Entry start (in {gs:%H:%M}-{ge:%H:%M})",
                            gs.strftime("%H:%M"))
        if s_raw is None:
            return
        start = parse_user_ts(s_raw, base=gs)
        e_raw = self.prompt("Entry end", ge.strftime("%H:%M"))
        if e_raw is None:
            return
        end = parse_user_ts(e_raw, base=gs)
        if start is None or end is None:
            self.message = "Invalid time"
            return
        if not (gs <= start < end <= ge):
            self.message = f"Entry must be within the gap {gs:%H:%M}-{ge:%H:%M}"
            return
        project = self._choose_project()
        if project is None:
            return
        task = self._choose_task(project, default_name="")
        if task is None:
            return
        self.checkpoint()
        clock = ClockEntry(start=start, end=end)
        task.clocks.append(clock)
        task.collapsed = False
        self.doc.touch(clock)
        self.doc.save()
        self.message = f"Added {start:%H:%M}-{end:%H:%M} to {task.name}"

    # -- calendar import ---------------------------------------------------

    def import_calendar(self) -> None:
        csv_path = self.prompt("Import calendar CSV (path)")
        if csv_path is None or not csv_path.strip():
            return
        try:
            text = Path(csv_path.strip()).read_text(encoding="utf-8-sig")
        except OSError as exc:
            self.message = f"Could not read CSV: {exc}"
            return
        events, issues = parse_csv(text)
        if not events:
            self.show_report("Calendar import", issues or ["No events found."],
                             plain=True)
            return

        raw_code = self.prompt("Blackout status code (Out of Office)", "4")
        if raw_code is None:
            return
        try:
            blackout_code = int(raw_code.strip())
        except ValueError:
            blackout_code = 4

        starts = [e.start.date() for e in events]
        start = self.prompt_date("Import start")
        if start is _CANCEL:
            return
        if start is None:
            start = min(starts)
        end = self.prompt_date("Import end")
        if end is _CANCEL:
            return
        if end is None:
            end = max(starts)
        if end < start:
            self.message = "End date is before start date"
            return

        plan = plan_import(events, start, end, blackout_code)
        if not plan.candidates:
            self.show_report("Calendar import", [
                f"No import candidates in {start}..{end}.",
                f"({len(plan.ignored_blackout)} masked by blackout windows)"]
                + issues, plain=True)
            return

        make_backup(self.doc.path)
        self.checkpoint()
        self._run_import(plan, blackout_code)

    def _run_import(self, plan, blackout_code: int) -> None:
        all_targets: dict = {}      # subject -> (project, task) for "all"
        ignore_subjects: set = set()
        imported = skipped = ignored = dup = 0

        # most-recent-first: if interrupted partway through, the events
        # closest to today are the ones already handled
        for event in reversed(plan.candidates):
            if event.subject in ignore_subjects:
                ignored += 1
                continue
            if self.doc.find_clock(event.start, event.end) is not None:
                dup += 1                 # already imported earlier — skip
                continue

            if event.subject in all_targets:
                project, task = all_targets[event.subject]
                if self._import_event(event, project, task):
                    imported += 1
                else:
                    skipped += 1
                continue

            choice = self._import_choice(event, blackout_code)
            if choice == "quit":
                break
            if choice == "skip":
                skipped += 1
                continue
            if choice == "ignore_all":
                ignore_subjects.add(event.subject)
                ignored += 1
                continue
            # keep or all -> pick destination
            project = self._choose_project()
            if project is None:
                skipped += 1
                continue
            task = self._choose_task(project, default_name=event.subject)
            if task is None:
                skipped += 1
                continue
            if choice == "all":
                all_targets[event.subject] = (project, task)
            if self._import_event(event, project, task):
                imported += 1
            else:
                skipped += 1

        self.save_and_refresh()
        self.message = (f"Import done: {imported} added, {skipped} skipped, "
                        f"{ignored} ignored, {dup} duplicate(s)")

    def _import_choice(self, event, blackout_code: int) -> str:
        lines = [
            f"Subject : {event.subject}",
            f"When    : {event.start:%Y-%m-%d %H:%M}--{event.end:%Y-%m-%d %H:%M}",
            f"Status  : {event.status_label(blackout_code)}",
            "",
            "k = keep (import this one)",
            "a = all (import every entry with this subject)",
            "s = skip this entry",
            "i = ignore all with this subject",
            "q = quit importing",
        ]
        win = self._centered_win(len(lines) + 4,
                                 max(46, max(len(s) for s in lines) + 6))
        h, w = win.getmaxyx()
        win.addstr(0, 2, " Calendar entry ")
        for i, line in enumerate(lines):
            win.addstr(i + 1, 2, line[: w - 4])
        win.addstr(h - 1, 2, "k/a/s/i/q"[: w - 4])
        win.refresh()
        keymap = {"k": "keep", "a": "all", "s": "skip", "i": "ignore_all",
                  "q": "quit"}
        while True:
            ch = win.get_wch()
            if isinstance(ch, str) and ch in keymap:
                return keymap[ch]
            if ch == "\x1b":
                return "quit"

    def _choose_project(self):
        names = ["+ New project"] + [p.name for p in self.doc.projects]
        idx = self.prompt_list_choice("Import into project", names, 0)
        if idx is None:
            return None
        if idx == 0:  # New at the top
            name = self.prompt("New project name")
            if name is None or not name.strip():
                return None
            now = self._now()
            project = Project(name=name.strip(), created=now, modified=now)
            self.doc.projects.append(project)
            return project
        return self.doc.projects[idx - 1]

    def _choose_task(self, project, default_name: str):
        names = ["+ New task"] + [t.name for t in project.tasks]
        idx = self.prompt_list_choice(f"Task in {project.name}", names, 0)
        if idx is None:
            return None
        if idx == 0:  # New at the top
            name = self.prompt("New task name", default_name)
            if name is None or not name.strip():
                return None
            now = self._now()
            task = Task(name=name.strip(), created=now, modified=now)
            project.tasks.append(task)
            project.modified = now
            project.collapsed = False
            return task
        return project.tasks[idx - 1]

    def _import_event(self, event, project, task) -> bool:
        """Write one calendar event as a clock on ``task``.  Returns True if
        imported, False if the user skipped it at the overlap popup."""
        clock = ClockEntry(start=event.start, end=event.end)
        task.clocks.append(clock)
        changes = overlap_changes(self.doc, clock, event.start, event.end)
        if not changes:
            self.doc.touch(clock)
            return True

        pieces = reshape_to_avoid(self.doc, clock, event.start, event.end)

        def fmt(s, e):
            return f"{s:%Y-%m-%d %H:%M}--{e:%Y-%m-%d %H:%M}"

        n = len(changes)
        lines = [f"'{event.subject}' overlaps {n} existing entr"
                 f"{'y' if n == 1 else 'ies'}.  Whose time wins?", ""]
        lines.append("[e] This imported time wins — the others are adjusted:")
        lines += ["      " + describe_change(c) for c in changes]
        lines += ["", "[o] The existing entries win — this import is adjusted:"]
        lines.append("      this time -> " + (
            "0:00 — WIPED OUT (possible typo)" if not pieces
            else "  +  ".join(fmt(s, e) for s, e in pieces)))
        lines += ["", "e = imported wins   o = existing win   Esc = skip entry"]

        choice = self.choose_precedence("Import overlap", lines)
        if choice is None:                 # Esc -> skip this entry
            task.clocks.remove(clock)
            return False
        if choice == "e":
            apply_overlap_changes(changes)
            for c in changes:
                self.doc.touch(c.clock)
            self.doc.touch(clock)
        else:                              # existing win: reshape the import
            apply_reshape(self.doc, clock, pieces, event.start)
            self.doc.touch(clock)
        return True

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

    def prompt_list_choice(self, label: str, options: list[str],
                           index: int = 0) -> int | None:
        """Vertical scrolling chooser (one option per line).

        Up/Down (or j/k) move, Enter selects, Esc cancels; g/G jump to the
        ends. Returns the chosen index, or None on Esc.
        """
        maxy, maxx = self.stdscr.getmaxyx()
        width = min(maxx - 2, max([len(label)] + [len(o) for o in options]) + 8)
        width = max(width, 34)
        height = min(maxy - 2, len(options) + 3)
        win = self._centered_win(height, width)
        h, w = win.getmaxyx()
        win.addstr(0, 2, f" {label[: w - 6]} ")
        view_h = h - 2                 # option rows (1 .. h-2); h-1 is the hint
        offset = 0
        while True:
            if index < offset:
                offset = index
            elif index >= offset + view_h:
                offset = index - view_h + 1
            for i in range(view_h):
                oi = offset + i
                win.addstr(i + 1, 1, " " * (w - 2))
                if oi < len(options):
                    attr = curses.A_REVERSE if oi == index else curses.A_NORMAL
                    marker = "> " if oi == index else "  "
                    win.addstr(i + 1, 2, (marker + options[oi])[: w - 4], attr)
            more = "  (more)" if len(options) > view_h else ""
            win.addstr(h - 1, 2, ("Up/Down select, Esc cancel" + more)[: w - 4])
            win.refresh()
            ch = win.get_wch()
            if ch in (curses.KEY_UP, "k"):
                index = (index - 1) % len(options)
            elif ch in (curses.KEY_DOWN, "j"):
                index = (index + 1) % len(options)
            elif ch == curses.KEY_NPAGE:
                index = min(len(options) - 1, index + view_h)
            elif ch == curses.KEY_PPAGE:
                index = max(0, index - view_h)
            elif ch in (curses.KEY_HOME, "g"):
                index = 0
            elif ch in (curses.KEY_END, "G"):
                index = len(options) - 1
            elif ch in ("\n", "\r", curses.KEY_ENTER):
                return index
            elif ch == "\x1b":
                return None

    def edit_multiline(self, title: str, initial: str = "") -> str | None:
        """Full multi-line text editor.

        Maintains a list of lines and a (row, col) cursor so that Enter
        splits the current line at the cursor (inserting a blank line in the
        middle works), Backspace joins with the previous line at column 0,
        etc.  Ctrl+O saves, Esc cancels (returns None).

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
            return ch in ("\x0f", 15)       # Ctrl+O (output/save)

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
                hint = "Enter: newline  Ctrl+O: save  Esc: cancel"
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

    def confirm_list(self, title: str, lines: list[str]) -> bool:
        """Scrollable list with a y/n prompt.  Returns True only on y."""
        maxy, maxx = self.stdscr.getmaxyx()
        width = min(maxx - 2, max([len(title)] + [len(s) for s in lines]) + 6)
        width = max(width, 40)
        height = min(maxy - 2, len(lines) + 4)
        win = self._centered_win(height, width)
        h, w = win.getmaxyx()
        win.addstr(0, 2, f" {title[: w - 6]} ")
        offset = 0
        body = h - 3
        while True:
            for i in range(body):
                win.addstr(i + 1, 2, " " * (w - 4))
                if offset + i < len(lines):
                    win.addstr(i + 1, 2, str(lines[offset + i])[: w - 4])
            footer = "y = yes, n/esc = no" + (
                "   (Up/Down scroll)" if len(lines) > body else "")
            win.addstr(h - 1, 2, footer[: w - 4])
            win.refresh()
            ch = win.get_wch()
            if ch == curses.KEY_DOWN and offset + body < len(lines):
                offset += 1
            elif ch == curses.KEY_UP and offset > 0:
                offset -= 1
            elif ch in ("y", "Y"):
                return True
            elif ch in ("n", "N", "\x1b"):
                return False

    def choose_precedence(self, title: str, lines: list[str]) -> str | None:
        """Scrollable list with an e/o/Esc choice.

        Returns "e" (edited wins), "o" (others win), or None (cancel).
        """
        maxy, maxx = self.stdscr.getmaxyx()
        width = min(maxx - 2, max([len(title)] + [len(s) for s in lines]) + 6)
        width = max(width, 40)
        height = min(maxy - 2, len(lines) + 4)
        win = self._centered_win(height, width)
        h, w = win.getmaxyx()
        win.addstr(0, 2, f" {title[: w - 6]} ")
        offset = 0
        body = h - 3
        while True:
            for i in range(body):
                win.addstr(i + 1, 2, " " * (w - 4))
                if offset + i < len(lines):
                    win.addstr(i + 1, 2, str(lines[offset + i])[: w - 4])
            footer = "e/o choose, Esc cancel" + (
                "   (Up/Down scroll)" if len(lines) > body else "")
            win.addstr(h - 1, 2, footer[: w - 4])
            win.refresh()
            ch = win.get_wch()
            if ch == curses.KEY_DOWN and offset + body < len(lines):
                offset += 1
            elif ch == curses.KEY_UP and offset > 0:
                offset -= 1
            elif ch in ("e", "E"):
                return "e"
            elif ch in ("o", "O"):
                return "o"
            elif ch == "\x1b":
                return None

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
