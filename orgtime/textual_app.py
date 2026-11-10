"""Textual front-end for orgtime.

Run with::

    python -m orgtime.textual_app [path/to/file.org]

It reuses :mod:`orgtime.model` (parsing, clocking, soft-delete, consistency
checks) and :mod:`orgtime.view` (row flattening / labels) unchanged, so the
file format and behaviour match the (now legacy) curses version. Dialogs
are built from Textual's own widgets (``Input``, ``OptionList``,
``SelectionList``, ``TextArea``) instead of hand-rolled curses windows;
everything else mirrors ``legacy/curses_app.py`` action-for-action.
"""

from __future__ import annotations

import copy
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, OptionList, SelectionList, Static, TextArea
from textual.widgets.option_list import Option

from .model import (
    CLOSED_STATUSES,
    EXPAND_COLLAPSED,
    EXPAND_FULL,
    EXPAND_PARTIAL,
    EXPAND_STATES,
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
    human_duration,
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
from .notes import note_header, note_path
from .report import build_report, default_filename
from .view import (
    COMMENT,
    ENTRY,
    GAP,
    MORE,
    PROJECT,
    SORT_MODES,
    TASK,
    CommentRef,
    HELP_LINES,
    MoreRef,
    describe_deleted_item,
    flatten,
    next_match_index,
    priority_rows,
    search_targets,
    sorted_clocks,
    timeline_hidden_counts,
    timeline_rows,
)

UNDO_LIMIT = 100
_CANCEL = object()  # sentinel: user pressed Esc in a prompt
DEFAULT_HINT = "?: help   q: quit   N/n: new   e: edit   d: del   i/o: clock"


def _row_style(row) -> str:
    """Rich style string for one Row, mirroring curses_app._row_attr()."""
    if row.warn:
        return "bold red"
    if row.kind == PROJECT:
        if row.stale == "ancient":
            return "bold dim white"
        if row.stale == "stale":
            return "bold dim green"
        return "bold cyan"
    if row.kind == TASK:
        if row.running:
            return "bold yellow"
        if row.stale == "ancient":
            return "dim white"
        if row.stale == "stale":
            return "dim green"
        return "yellow"
    if row.kind == COMMENT:
        return "green"
    if row.kind == MORE:
        return "dim green"
    return ""


class RowList(OptionList):
    """OptionList with vim-style extra navigation bindings."""

    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("g", "first", show=False),
        Binding("G", "last", show=False),
    ]


# -- small dialog screens ---------------------------------------------------


class PromptScreen(ModalScreen[str | None]):
    """Single-line text input.  Dismisses with the string, or None on Esc."""

    DEFAULT_CSS = """
    PromptScreen { align: center middle; }
    PromptScreen > Vertical {
        width: auto; min-width: 44; height: auto;
        border: round $primary; padding: 1 2;
    }
    PromptScreen Static { margin-bottom: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, label: str, initial: str = "") -> None:
        super().__init__()
        self.label = label
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.label)
            yield Input(value=self.initial)

    def on_mount(self) -> None:
        inp = self.query_one(Input)
        inp.cursor_position = len(inp.value)
        inp.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ListChoiceScreen(ModalScreen[int | None]):
    """Scrolling single-choice list.  Dismisses with the chosen index, or
    None on Esc."""

    DEFAULT_CSS = """
    ListChoiceScreen { align: center middle; }
    ListChoiceScreen > Vertical {
        width: auto; min-width: 40; height: auto; max-height: 80%;
        border: round $primary; padding: 1 2;
    }
    ListChoiceScreen Static { margin-bottom: 1; }
    ListChoiceScreen OptionList { height: auto; max-height: 20; }
    """
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, label: str, options: list[str], index: int = 0) -> None:
        super().__init__()
        self.label = label
        self.options = options
        self.index = index

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.label)
            yield OptionList(*self.options)

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        ol.highlighted = self.index
        ol.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option_index)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ChecklistList(SelectionList):
    """SelectionList where Enter confirms instead of toggling (space still
    toggles)."""

    BINDINGS = [Binding("enter", "confirm_checklist", show=False)]

    def action_confirm_checklist(self) -> None:
        self.post_message(ChecklistList.Confirmed(self, self.highlighted or 0))

    class Confirmed(SelectionList.SelectionMessage):
        pass


class ChecklistScreen(ModalScreen[list[int] | None]):
    """Scrolling multi-choice checklist.  Space toggles, Enter confirms
    (dismisses with the sorted list of selected indices), Esc cancels."""

    DEFAULT_CSS = """
    ChecklistScreen { align: center middle; }
    ChecklistScreen > Vertical {
        width: auto; min-width: 44; height: auto; max-height: 80%;
        border: round $primary; padding: 1 2;
    }
    ChecklistScreen Static { margin-bottom: 1; }
    ChecklistScreen SelectionList { height: auto; max-height: 20; }
    """
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, label: str, options: list[str]) -> None:
        super().__init__()
        self.label = label
        self.options = options

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.label)
            yield ChecklistList(*[(opt, i) for i, opt in enumerate(self.options)])

    def on_mount(self) -> None:
        self.query_one(ChecklistList).focus()

    def on_checklist_list_confirmed(self, event: ChecklistList.Confirmed) -> None:
        event.stop()
        self.dismiss(sorted(self.query_one(ChecklistList).selected))

    def action_cancel(self) -> None:
        self.dismiss(None)


class MultilineScreen(ModalScreen[str | None]):
    """Multi-line text editor (comments).  Ctrl+O saves, Esc cancels."""

    DEFAULT_CSS = """
    MultilineScreen { align: center middle; }
    MultilineScreen > Vertical {
        width: 80%; max-width: 90; height: 60%;
        border: round $primary; padding: 1 2;
    }
    MultilineScreen Static { margin-bottom: 1; }
    MultilineScreen TextArea { height: 1fr; }
    """
    BINDINGS = [
        Binding("escape", "cancel", show=False),
        Binding("ctrl+o", "save", show=False),
    ]

    def __init__(self, title: str, initial: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"{self.title_text}  (Ctrl+O save, Esc cancel)")
            yield TextArea(self.initial)

    def on_mount(self) -> None:
        self.query_one(TextArea).focus()

    def action_save(self) -> None:
        self.dismiss(self.query_one(TextArea).text)

    def action_cancel(self) -> None:
        self.dismiss(None)


class _ScrollKeyScreen(ModalScreen):
    """Base for the small fixed-choice dialogs below: a title, a scrollable
    block of text lines, and up/down scrolling on every unhandled key."""

    DEFAULT_CSS = """
    _ScrollKeyScreen { align: center middle; }
    _ScrollKeyScreen > Vertical {
        width: 80%; max-width: 100; height: auto; max-height: 80%;
        border: round $primary; padding: 1 2;
    }
    _ScrollKeyScreen Static#dlg-title { margin-bottom: 1; }
    _ScrollKeyScreen Static#dlg-footer { margin-top: 1; }
    _ScrollKeyScreen VerticalScroll { height: auto; max-height: 20; }
    """

    footer_hint = ""

    def __init__(self, title: str, lines: list) -> None:
        super().__init__()
        self.title_text = title
        self.lines = lines

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, id="dlg-title")
            with VerticalScroll():
                yield Static("\n".join(str(line) for line in self.lines))
            yield Static(self.footer_hint, id="dlg-footer")

    def _scroll(self, key: str) -> bool:
        body = self.query_one(VerticalScroll)
        if key == "down":
            body.scroll_down()
            return True
        if key == "up":
            body.scroll_up()
            return True
        return False


class ConfirmScreen(ModalScreen[bool]):
    """Single short yes/no message.  Any key dismisses; y/Y -> True."""

    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > Vertical {
        width: auto; min-width: 40; height: auto;
        border: round $primary; padding: 1 2;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.message)
            yield Static("y = yes, any other key = no", id="dlg-footer")

    def on_key(self, event) -> None:
        event.stop()
        self.dismiss(event.key in ("y", "Y"))


class ConfirmListScreen(_ScrollKeyScreen):
    """Scrollable y/n confirmation.  Only y/Y/n/N/Esc dismiss."""

    footer_hint = "y = yes, n/esc = no   (Up/Down scroll)"

    def on_key(self, event) -> None:
        if self._scroll(event.key):
            event.stop()
        elif event.key in ("y", "Y"):
            event.stop()
            self.dismiss(True)
        elif event.key in ("n", "N", "escape"):
            event.stop()
            self.dismiss(False)


class PrecedenceScreen(_ScrollKeyScreen):
    """Scrollable e/o/Esc choice (overlap resolution)."""

    footer_hint = "e/o choose, Esc cancel   (Up/Down scroll)"

    def on_key(self, event) -> None:
        if self._scroll(event.key):
            event.stop()
        elif event.key in ("e", "E"):
            event.stop()
            self.dismiss("e")
        elif event.key in ("o", "O"):
            event.stop()
            self.dismiss("o")
        elif event.key == "escape":
            event.stop()
            self.dismiss(None)


class ReportScreen(_ScrollKeyScreen):
    """Scrollable read-only report.  Any key (other than scrolling) closes."""

    footer_hint = "Up/Down scroll, any other key close"

    def on_key(self, event) -> None:
        if self._scroll(event.key):
            event.stop()
        else:
            event.stop()
            self.dismiss(None)


class ImportChoiceScreen(ModalScreen[str]):
    """Per-event calendar import choice: k/a/s/i/q (Esc = quit)."""

    DEFAULT_CSS = """
    ImportChoiceScreen { align: center middle; }
    ImportChoiceScreen > Vertical {
        width: auto; min-width: 46; height: auto;
        border: round $primary; padding: 1 2;
    }
    """
    _KEYMAP = {"k": "keep", "a": "all", "s": "skip", "i": "ignore_all", "q": "quit"}

    def __init__(self, event, blackout_code: int) -> None:
        super().__init__()
        self.event = event
        self.blackout_code = blackout_code

    def compose(self) -> ComposeResult:
        e = self.event
        lines = [
            f"Subject : {e.subject}",
            f"When    : {e.start:%Y-%m-%d %H:%M}--{e.end:%Y-%m-%d %H:%M}",
            f"Status  : {e.status_label(self.blackout_code)}",
            "",
            "k = keep (import this one)",
            "a = all (import every entry with this subject)",
            "s = skip this entry",
            "i = ignore all with this subject",
            "q = quit importing",
        ]
        with Vertical():
            yield Static(" Calendar entry ", id="dlg-title")
            yield Static("\n".join(lines))

    def on_key(self, event) -> None:
        if event.key in self._KEYMAP:
            event.stop()
            self.dismiss(self._KEYMAP[event.key])
        elif event.key == "escape":
            event.stop()
            self.dismiss("quit")


# -- main application --------------------------------------------------------


class OrgtimeApp(App):
    CSS = """
    Screen { layout: vertical; }
    RowList { height: 1fr; border: none; }
    #clockbar { height: 1; background: $primary-darken-2; color: white; }
    #statusbar { height: 1; }
    """

    BINDINGS = [
        Binding("space", "toggle_collapse", show=False),
        Binding("tab", "toggle_collapse", show=False),
        Binding("J", "jump_running", show=False),
        Binding("C", "collapse_all", show=False),
        Binding("z", "cycle_sort", show=False),
        Binding("A", "import_calendar", show=False),
        Binding("t", "timeline", show=False),
        Binding("p", "priority", show=False),
        Binding("slash", "search", show=False),
        Binding("m", "move_task", show=False),
        Binding("M", "merge_tasks", show=False),
        Binding("N", "new_project", show=False),
        Binding("n", "new_task", show=False),
        Binding("e", "edit", show=False),
        Binding("d", "delete", show=False),
        Binding("c", "comment", show=False),
        Binding("X", "expunge", show=False),
        Binding("R", "restore_deleted", show=False),
        Binding("H", "snap_overlaps", show=False),
        Binding("i", "clock_in", show=False),
        Binding("o", "clock_out", show=False),
        Binding("I", "clock_in_at", show=False),
        Binding("O", "clock_out_at", show=False),
        Binding("s", "cycle_status(1)", show=False),
        Binding("S", "cycle_status(-1)", show=False),
        Binding("D", "mark_done", show=False),
        Binding("1", "set_priority(1)", show=False),
        Binding("2", "set_priority(2)", show=False),
        Binding("3", "set_priority(3)", show=False),
        Binding("4", "set_priority(4)", show=False),
        Binding("5", "set_priority(5)", show=False),
        Binding("u", "undo", show=False),
        Binding("U", "redo", show=False),
        Binding("v", "check", show=False),
        Binding("V", "open_note", show=False),
        Binding("r", "report", show=False),
        Binding("L", "reload", show=False),
        Binding("question_mark", "show_help", show=False),
        Binding("q", "quit_app", show=False),
    ]

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.doc, self.load_issues = load(path)
        self.rows: list = []
        self.message = ""
        self._undo: list[Document] = []
        self._redo: list[Document] = []
        self.search_term = ""
        self.sort_mode = "file"
        self.config = load_config(path)

    # -- setup ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield RowList(id="main-list")
        yield Static(id="clockbar")
        yield Static(id="statusbar")

    @work
    async def on_mount(self) -> None:
        make_backup(self.doc.path)
        self.query_one("#main-list", RowList).focus()
        self.refresh_rows()
        if self.load_issues:
            await self.push_screen_wait(
                ReportScreen("Problems while loading file", self.load_issues))

    def _now(self) -> datetime:
        return datetime.now().replace(second=0, microsecond=0)

    # -- model/view sync -------------------------------------------------

    def refresh_rows(self) -> None:
        prev = self.selected_obj()
        prev_owner = getattr(prev, "owner", None)
        prev_type = type(prev)
        self.rows = flatten(self.doc, sort_mode=self.sort_mode)
        row_list = self.query_one("#main-list", RowList)
        row_list.clear_options()
        for row in self.rows:
            row_list.add_option(Option(Text(row.text, style=_row_style(row))))
        cursor = 0
        if prev is not None:
            for i, row in enumerate(self.rows):
                obj = row.obj
                if obj is prev or (
                    prev_owner is not None and type(obj) is prev_type
                    and getattr(obj, "owner", None) is prev_owner
                ):
                    cursor = i
                    break
        if self.rows:
            row_list.highlighted = max(0, min(cursor, len(self.rows) - 1))
        self._update_bars()

    def selected_obj(self):
        row_list = self.query_one("#main-list", RowList)
        idx = row_list.highlighted
        if idx is not None and 0 <= idx < len(self.rows):
            return self.rows[idx].obj
        return None

    def selected_item(self):
        obj = self.selected_obj()
        return obj.owner if isinstance(obj, (CommentRef, MoreRef)) else obj

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

    def _select_obj(self, obj) -> bool:
        row_list = self.query_one("#main-list", RowList)
        for i, row in enumerate(self.rows):
            if row.obj is obj:
                row_list.highlighted = i
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

    # -- bars ------------------------------------------------------------

    def _update_bars(self) -> None:
        active = self.doc.running()
        if active:
            project, task, clock = active
            text = (f" RUNNING {human_duration(clock.duration())} on "
                    f"{task.name} ({project.name}, since "
                    f"{clock.start.strftime('%H:%M')}) ")
        else:
            text = " no clock running "
        self.query_one("#clockbar", Static).update(text)
        self.query_one("#statusbar", Static).update(self.message or DEFAULT_HINT)

    def _set_message(self, text: str) -> None:
        self.message = text
        self._update_bars()

    def _begin(self) -> None:
        self.message = ""
        self._update_bars()

    # -- undo --------------------------------------------------------------

    def checkpoint(self) -> None:
        self._undo.append(copy.deepcopy(self.doc))
        del self._undo[:-UNDO_LIMIT]
        self._redo.clear()

    def action_undo(self) -> None:
        self._begin()
        if not self._undo:
            self._set_message("Nothing to undo")
            return
        self._redo.append(self.doc)
        self.doc = self._undo.pop()
        self.save_and_refresh()
        self._set_message(f"Undone ({len(self._undo)} more, U to redo)")

    def action_redo(self) -> None:
        self._begin()
        if not self._redo:
            self._set_message("Nothing to redo")
            return
        self._undo.append(self.doc)
        self.doc = self._redo.pop()
        self.save_and_refresh()
        self._set_message("Redone")

    # -- quit --------------------------------------------------------------

    def action_quit_app(self) -> None:
        self.doc.save()
        self.exit()

    # -- main-list selection / collapse -------------------------------------

    @on(OptionList.OptionSelected, "#main-list")
    def _on_main_list_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_toggle_collapse()

    def _related_clock(self, raw) -> ClockEntry | None:
        if isinstance(raw, ClockEntry):
            return raw
        if isinstance(raw, CommentRef) and isinstance(raw.owner, ClockEntry):
            return raw.owner
        return None

    def _reveal(self, task: Task, clock: ClockEntry | None = None) -> None:
        if task.expand == EXPAND_FULL:
            return
        if clock is not None:
            latest = sorted_clocks(task)[0] if task.clocks else None
            if clock is not latest:
                task.expand = EXPAND_FULL
                return
        if task.expand == EXPAND_COLLAPSED:
            task.expand = EXPAND_PARTIAL

    def action_toggle_collapse(self) -> None:
        raw = self.selected_obj()
        task = self.selected_task()
        if task is not None:
            clocks = sorted_clocks(task)
            latest = clocks[0] if clocks else None
            clock = self._related_clock(raw)
            if clock is not None and clock is not latest:
                task.expand = EXPAND_PARTIAL
                self.refresh_rows()
                self._select_obj(latest)
                return
            i = EXPAND_STATES.index(task.expand)
            task.expand = EXPAND_STATES[(i + 1) % len(EXPAND_STATES)]
            self.refresh_rows()
            if task.expand == EXPAND_COLLAPSED or (
                    task.expand == EXPAND_FULL and isinstance(raw, MoreRef)):
                self._select_obj(task)
            return
        obj = self.selected_item()
        if isinstance(obj, Project):
            obj.collapsed = not obj.collapsed
            self.refresh_rows()

    def action_jump_running(self) -> None:
        self._begin()
        active = self.doc.running()
        if active is None:
            self._set_message("No clock is running")
            return
        project, task, clock = active
        project.collapsed = False
        self._reveal(task, clock)
        self.refresh_rows()
        self._select_obj(clock)
        self._set_message(f"Jumped to running clock on {task.name}")

    def action_collapse_all(self) -> None:
        self._begin()
        project = self._project_of_selection()
        for p in self.doc.projects:
            p.collapsed = True
        self.refresh_rows()
        if project is not None:
            self._select_obj(project)
        self._set_message("Collapsed all projects")

    def action_cycle_sort(self) -> None:
        self._begin()
        self.sort_mode = SORT_MODES[
            (SORT_MODES.index(self.sort_mode) + 1) % len(SORT_MODES)]
        self.refresh_rows()
        labels = {"file": "file order", "priority": "priority (1 first)",
                  "created": "created (oldest first)",
                  "modified": "modified (newest first)"}
        self._set_message(f"Sort: {labels[self.sort_mode]}")

    # -- search ------------------------------------------------------------

    @work
    async def action_search(self) -> None:
        self._begin()
        term = await self.push_screen_wait(
            PromptScreen("Search (substring)", self.search_term))
        if term is None:
            return
        term = term.strip()
        if not term:
            return
        self.search_term = term
        self._do_search()

    def _current_target_index(self, targets) -> int:
        obj = self.selected_obj()
        owner = obj.owner if isinstance(obj, (CommentRef, MoreRef)) else obj
        for i, target in enumerate(targets):
            if target.owner is owner:
                return i
        return -1

    def _do_search(self) -> None:
        targets = search_targets(self.doc)
        if not targets:
            self._set_message("Nothing to search")
            return
        idx = next_match_index(targets, self.search_term,
                               self._current_target_index(targets))
        if idx is None:
            self._set_message(f"No match for {self.search_term!r}")
            return
        self._reveal_and_select(targets[idx])
        self._set_message(f"Search: {self.search_term}  (/ to repeat)")

    def _reveal_and_select(self, target) -> None:
        target.project.collapsed = False
        if target.kind == COMMENT and target.task is not None:
            clock = target.owner if isinstance(target.owner, ClockEntry) else None
            self._reveal(target.task, clock)
        self.refresh_rows()
        if target.kind == COMMENT:
            for i, row in enumerate(self.rows):
                if row.kind == COMMENT and row.obj.owner is target.owner:
                    self.query_one("#main-list", RowList).highlighted = i
                    return
        else:
            self._select_obj(target.owner)

    # -- move a task to another project ------------------------------------

    @work
    async def action_move_task(self) -> None:
        self._begin()
        task = self.selected_task()
        if task is None:
            self._set_message("Select a task to move")
            return
        src = self.doc.project_of(task)
        others = sorted((p for p in self.doc.projects if p is not src),
                        key=lambda p: p.name.lower())
        if not others:
            self._set_message("No other project to move to")
            return
        idx = await self.push_screen_wait(
            ListChoiceScreen(f"Move '{task.name}' to project",
                             [p.name for p in others], 0))
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
        self._set_message(f"Moved '{task.name}' to {dest.name}")

    # -- merge tasks together ------------------------------------------------

    @work
    async def action_merge_tasks(self) -> None:
        self._begin()
        task = self.selected_task()
        if task is None:
            self._set_message("Select a task to merge into")
            return
        project = self.doc.project_of(task)
        others = sorted((t for t in project.tasks if t is not task),
                        key=lambda t: t.name.lower()) if project else []
        if not others:
            self._set_message("No other task in this project to merge")
            return
        idxs = await self.push_screen_wait(
            ChecklistScreen(f"Merge into '{task.name}'",
                            [t.name for t in others]))
        if not idxs:
            self._set_message("Merge cancelled")
            return
        sources = [others[i] for i in idxs]
        names = ", ".join(f"'{t.name}'" for t in sources)
        ok = await self.push_screen_wait(ConfirmScreen(
            f"Merge {names} into '{task.name}'? Their time entries and "
            f"comments move over; the emptied task(s) are deleted."))
        if not ok:
            return
        self.checkpoint()
        self.doc.merge_tasks(task, sources, self._now())
        self.save_and_refresh()
        self._select_obj(task)
        self._set_message(f"Merged {len(sources)} task(s) into '{task.name}'")

    # -- external notes (Obsidian/VS Code) -----------------------------------

    def _launch_editor(self, path: Path) -> bool:
        code_exe = shutil.which("code")
        if code_exe is None:
            return False
        try:
            subprocess.Popen([code_exe, str(path)])
        except OSError:
            return False
        return True

    @work
    async def action_open_note(self) -> None:
        self._begin()
        if not self.config.notes_dir:
            folder = await self.push_screen_wait(PromptScreen(
                "Notes folder (Obsidian vault, or any folder of .md files)"))
            if folder is None or not folder.strip():
                return
            self.config.notes_dir = folder.strip()
            save_config(self.doc.path, self.config)

        task = self.selected_task()
        if task is not None:
            project = self.doc.project_of(task)
        else:
            project = await self._choose_project()
            if project is None:
                return
            task = await self._choose_task(project, default_name="")
            if task is None:
                return
            self.save_and_refresh()
            self._select_obj(task)

        notes_dir = Path(self.config.notes_dir)
        try:
            notes_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._set_message(f"Can't create notes folder: {exc}")
            return

        dest = note_path(notes_dir, task.name)
        if not dest.exists():
            try:
                dest.write_text(
                    note_header(dest.stem, project.name, task.name, self._now()),
                    encoding="utf-8")
            except OSError as exc:
                self._set_message(f"Can't create note: {exc}")
                return

        if self._launch_editor(dest):
            self._set_message(f"Opened note: {dest.name}")
        else:
            self._set_message(f"Wrote {dest.name} — 'code' not found on PATH")

    # -- actions: items ----------------------------------------------------

    @work
    async def action_new_project(self) -> None:
        self._begin()
        name = await self.push_screen_wait(PromptScreen("New project name"))
        if name is None or not name.strip():
            return
        self.checkpoint()
        now = self._now()
        project = Project(name=name.strip(), created=now, modified=now)
        self.doc.projects.append(project)
        self.save_and_refresh()
        self._select_obj(project)
        self._set_message("Created project (1-5 sets priority)")

    @work
    async def action_new_task(self) -> None:
        self._begin()
        obj = self.selected_item()
        project = obj if isinstance(obj, Project) else (
            self.doc.project_of(t) if (t := self.selected_task()) else None)
        if project is None:
            self._set_message("Select a project first")
            return
        name = await self.push_screen_wait(PromptScreen(f"New task in {project.name}"))
        if name is None or not name.strip():
            return
        self.checkpoint()
        now = self._now()
        task = Task(name=name.strip(), created=now, modified=now)
        project.tasks.append(task)
        project.modified = now
        project.collapsed = False
        self.save_and_refresh()
        self._select_obj(task)
        self._set_message("Created task (1-5 priority, s/S/D status)")

    @work
    async def action_edit(self) -> None:
        self._begin()
        obj = self.selected_obj()
        if isinstance(obj, CommentRef):
            await self.edit_comment(obj.owner)
        elif isinstance(obj, ClockEntry):
            await self.edit_clock(obj)
        elif isinstance(obj, (Project, Task)):
            kind = "task" if isinstance(obj, Task) else "project"
            name = await self.push_screen_wait(PromptScreen(f"Rename {kind}", obj.name))
            if name is None or not name.strip():
                return
            created = await self.push_screen_wait(PromptScreen(
                "Created (YYYY-MM-DD HH:MM, blank = keep)",
                obj.created.strftime("%Y-%m-%d %H:%M") if obj.created else ""))
            if created is None:
                return
            self.checkpoint()
            obj.name = name.strip()
            if created.strip():
                new_created = parse_user_ts(created)
                if new_created is None:
                    self._set_message("Invalid created time; name updated, date kept")
                else:
                    obj.created = new_created
            self.doc.touch(obj)
            self.save_and_refresh()
        else:
            self._set_message("Nothing to edit")

    async def edit_clock(self, clock: ClockEntry) -> None:
        start = await self.push_screen_wait(
            PromptScreen("Start time", clock.start.strftime("%Y-%m-%d %H:%M")))
        if start is None:
            return
        start_dt = parse_user_ts(start)
        if start_dt is None:
            self._set_message("Invalid start time")
            return
        end_default = clock.end.strftime("%Y-%m-%d %H:%M") if clock.end else ""
        end = await self.push_screen_wait(
            PromptScreen("End time (blank = running)", end_default))
        if end is None:
            return
        end_dt = None
        if end.strip():
            end_dt = parse_user_ts(end, base=start_dt)
            if end_dt is None:
                self._set_message("Invalid end time")
                return
            if end_dt < start_dt:
                self._set_message("End is before start")
                return
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

        choice = await self.push_screen_wait(PrecedenceScreen("Resolve overlap", lines))
        if choice is None:
            self._set_message("Edit cancelled (overlap not resolved)")
            return
        self.checkpoint()
        if choice == "e":
            clock.start, clock.end = start_dt, end_dt
            apply_overlap_changes(changes)
            for c in changes:
                self.doc.touch(c.clock)
            self.doc.touch(clock)
            self._set_message(f"Edited time wins; adjusted {n} other entry(s)")
        else:
            apply_reshape(self.doc, clock, pieces, start_dt)
            self.doc.touch(clock)
            self._set_message("Others win; this time wiped to 0:00"
                              if not pieces else
                              f"Others win; this time reshaped into "
                              f"{len(pieces)} piece(s)")
        self.save_and_refresh()
        self.warn_about(clock)

    @work
    async def action_delete(self) -> None:
        self._begin()
        obj = self.selected_obj()
        if obj is None:
            self._set_message("Nothing selected")
            return
        if isinstance(obj, MoreRef):
            self._set_message("Nothing to delete here — press space to expand")
            return
        if isinstance(obj, Project):
            msg = f"Delete project '{obj.name}' and its {len(obj.tasks)} task(s)?"
        elif isinstance(obj, Task):
            msg = f"Delete task '{obj.name}'?"
        elif isinstance(obj, CommentRef):
            msg = "Delete this comment block?"
        else:
            msg = "Delete this clock entry (and its comments)?"
        ok = await self.push_screen_wait(ConfirmScreen(msg))
        if not ok:
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
        self._set_message("Deleted (kept as ## lines — press X to expunge, R to restore)")

    def _remove_clock(self, task: Task, clock: ClockEntry) -> None:
        task.tombstones.extend(soft_delete_lines(clock.lines(), self._now()))
        task.clocks.remove(clock)
        self.doc.touch(task)

    @work
    async def action_expunge(self) -> None:
        self._begin()
        count = self.doc.tombstone_count()
        if count == 0:
            self._set_message("No deleted lines to expunge")
            return
        ok = await self.push_screen_wait(
            ConfirmScreen(f"Permanently remove {count} deleted (##) line(s)?"))
        if not ok:
            return
        self.checkpoint()
        removed = self.doc.expunge()
        self.save_and_refresh()
        self._set_message(f"Expunged {removed} deleted line(s)")

    @work
    async def action_restore_deleted(self) -> None:
        self._begin()
        items = find_deleted_items(self.doc, self._now())
        if not items:
            self._set_message("No deleted items to restore")
            return
        options = [describe_deleted_item(it) for it in items]
        idx = await self.push_screen_wait(
            ListChoiceScreen("Restore (most recent deletion first)", options, 0))
        if idx is None:
            self._set_message("Restore cancelled")
            return
        item = items[idx]
        self.checkpoint()
        self.doc.restore(item)
        self.doc.touch(item.obj)
        if item.kind == "task":
            self._reveal(item.obj)
            item.owner.collapsed = False
        elif item.kind == "clock":
            self._reveal(item.owner, item.obj)
            project = self.doc.project_of(item.owner)
            if project is not None:
                project.collapsed = False
        self.save_and_refresh()
        self._select_obj(item.obj)
        self._set_message(f"Restored: {describe_deleted_item(item)}")

    # -- actions: comments -------------------------------------------------

    @work
    async def action_comment(self) -> None:
        self._begin()
        owner = self.selected_item()
        if owner is None:
            self._set_message("Select a line to comment on")
            return
        await self.edit_comment(owner)

    async def edit_comment(self, owner) -> None:
        text = await self.push_screen_wait(
            MultilineScreen("Comment", "\n".join(owner.comments)))
        if text is None:
            return
        lines = [ln.rstrip() for ln in text.splitlines()]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        self.checkpoint()
        owner.comments = lines
        if isinstance(owner, Project):
            owner.collapsed = False
        if isinstance(owner, Task):
            self._reveal(owner)
            self.doc.project_of(owner).collapsed = False
        elif isinstance(owner, ClockEntry):
            task = self.doc.task_of(owner)
            self._reveal(task, owner)
            self.doc.project_of(task).collapsed = False
        self.doc.touch(owner)
        self.save_and_refresh()

    # -- actions: clocking -------------------------------------------------

    async def action_clock_in(self) -> None:
        self._begin()
        task = self.selected_task()
        if task is None:
            self._set_message("Select a task to clock in")
            return
        self.checkpoint()
        self.doc.clock_in(task)
        self.doc.touch(task)
        self._reveal(task, task.clocks[-1])
        self.save_and_refresh()
        self._select_obj(task.clocks[-1])
        self._set_message(f"Clocked in: {task.name}")

    async def action_clock_out(self) -> None:
        self._begin()
        active = self.doc.running()
        if active is None:
            self._set_message("No clock is running")
            return
        _, task, clock = active
        self.checkpoint()
        self.doc.clock_out()
        self.doc.touch(task)
        self.save_and_refresh()
        self._set_message(f"Clocked out: {task.name}")
        self.warn_about(clock)

    async def _prompt_time(self, label: str, base: datetime | None = None) -> datetime | None:
        default = datetime.now().strftime("%Y-%m-%d %H:%M")
        while True:
            raw = await self.push_screen_wait(
                PromptScreen(f"{label} (YYYY-MM-DD HH:MM or HH:MM)", default))
            if raw is None:
                return None
            when = parse_user_ts(raw, base)
            if when is not None:
                return when
            self._set_message("Invalid time; try again")
            default = raw

    @work
    async def action_clock_in_at(self) -> None:
        self._begin()
        task = self.selected_task()
        if task is None:
            self._set_message("Select a task to clock in")
            return
        when = await self._prompt_time("Clock in at")
        if when is None:
            return
        self.checkpoint()
        self.doc.clock_in(task, when)
        self.doc.touch(task)
        self._reveal(task, task.clocks[-1])
        self.save_and_refresh()
        self._select_obj(task.clocks[-1])
        self._set_message(f"Clocked in: {task.name} at {when:%H:%M}")
        self.warn_about(task.clocks[-1])

    @work
    async def action_clock_out_at(self) -> None:
        self._begin()
        active = self.doc.running()
        if active is None:
            self._set_message("No clock is running")
            return
        _, task, clock = active
        when = await self._prompt_time("Clock out at", base=clock.start)
        if when is None:
            return
        if when < clock.start:
            self._set_message(f"End is before the clock start ({format_ts(clock.start)})")
            return
        self.checkpoint()
        clock.end = when
        self.doc.touch(task)
        self.save_and_refresh()
        self._set_message(f"Clocked out: {task.name} at {when:%H:%M}")
        self.warn_about(clock)

    def _set_status(self, task: Task, status: str) -> None:
        task.status = status
        if status in CLOSED_STATUSES and task.running_clock():
            self.doc.clock_out()

    def action_cycle_status(self, step: int = 1) -> None:
        self._begin()
        task = self.selected_task()
        if task is None:
            self._set_message("Select a task to change status")
            return
        self.checkpoint()
        self._set_status(
            task, STATUSES[(STATUSES.index(task.status) + step) % len(STATUSES)])
        self.doc.touch(task)
        self.save_and_refresh()

    @work
    async def action_mark_done(self) -> None:
        self._begin()
        obj = self.selected_item()
        if isinstance(obj, Project):
            n = len(obj.tasks)
            if n == 0:
                self._set_message("Project has no tasks")
                return
            ok = await self.push_screen_wait(
                ConfirmScreen(f"Mark all {n} task(s) in '{obj.name}' as DONE?"))
            if not ok:
                return
            self.checkpoint()
            for task in obj.tasks:
                self._set_status(task, "DONE")
                self.doc.touch(task)
            self.save_and_refresh()
            self._set_message(f"Marked {n} task(s) DONE in {obj.name}")
            return
        task = self.selected_task()
        if task is None:
            self._set_message("Select a task or project to mark DONE")
            return
        self.checkpoint()
        self._set_status(task, "DONE")
        self.doc.touch(task)
        self.save_and_refresh()
        self._set_message(f"Marked DONE: {task.name}")

    def action_set_priority(self, value: int) -> None:
        self._begin()
        obj = self.selected_item()
        if isinstance(obj, ClockEntry):
            obj = self.doc.task_of(obj)
        if not isinstance(obj, (Project, Task)):
            self._set_message("Select a project or task")
            return
        self.checkpoint()
        obj.priority = value
        self.doc.touch(obj)
        self.save_and_refresh()

    # -- actions: misc -----------------------------------------------------

    @work
    async def action_check(self) -> None:
        self._begin()
        problems = self.load_issues + check_consistency(self.doc)
        fixes = find_snap_fixes(self.doc)
        if fixes:
            problems = problems + [
                f"{len(fixes)} small overlap(s) could be snapped to a "
                f"half-hour — press H to review"]
        await self.push_screen_wait(
            ReportScreen("Consistency check", problems or ["No problems found."]))

    @work
    async def action_snap_overlaps(self) -> None:
        self._begin()
        fixes = find_snap_fixes(self.doc)
        if not fixes:
            self._set_message("No small overlaps bordering a half-hour found")
            return
        lines = [f"{len(fixes)} small overlap(s) border or contain a "
                 "half-hour mark:", ""]
        lines += ["  " + describe_snap_fix(f) for f in fixes]
        lines += ["", "Snap all of these to the half-hour?"]
        ok = await self.push_screen_wait(ConfirmListScreen("Snap overlaps to half-hour", lines))
        if not ok:
            self._set_message("Snap cancelled")
            return
        self.checkpoint()
        for fix in fixes:
            apply_snap_fix(fix)
            self.doc.touch(fix.clock_a)
            self.doc.touch(fix.clock_b)
        self.save_and_refresh()
        self._set_message(f"Snapped {len(fixes)} overlap(s) to the half-hour")

    async def _prompt_date(self, label: str):
        """Prompt for an optional date.  Returns a date, None (blank = open),
        or the _CANCEL sentinel if Esc was pressed."""
        default = ""
        while True:
            raw = await self.push_screen_wait(
                PromptScreen(f"{label} (YYYY-MM-DD, blank = open-ended)", default))
            if raw is None:
                return _CANCEL
            raw = raw.strip()
            if not raw:
                return None
            d = parse_user_date(raw)
            if d is not None:
                return d
            self._set_message("Invalid date; try again")
            default = raw

    @work
    async def action_report(self) -> None:
        self._begin()
        start = await self._prompt_date("Report start")
        if start is _CANCEL:
            return
        end = await self._prompt_date("Report end")
        if end is _CANCEL:
            return
        if start is not None and end is not None and end < start:
            self._set_message("End date is before start date")
            return
        default = default_filename(self.doc, start, end)
        name = await self.push_screen_wait(PromptScreen("Write report to file", default))
        if name is None:
            return
        name = name.strip() or default
        out_path = Path(name)
        if not out_path.is_absolute():
            base = self.doc.path.parent if self.doc.path else Path.cwd()
            out_path = base / out_path
        try:
            out_path.write_text(build_report(self.doc, start, end), encoding="utf-8")
        except OSError as exc:
            self._set_message(f"Could not write report: {exc}")
            return
        self._set_message(f"Wrote report to {out_path}")

    # -- timeline / priority screens -----------------------------------------

    def action_timeline(self) -> None:
        self.push_screen(TimelineScreen())

    def action_priority(self) -> None:
        if not priority_rows(self.doc, self._now()):
            self._begin()
            self._set_message("No open tasks to prioritize")
            return
        self.push_screen(PriorityScreen())

    # -- calendar import ---------------------------------------------------

    @work
    async def action_import_calendar(self) -> None:
        self._begin()
        csv_path = await self.push_screen_wait(PromptScreen("Import calendar CSV (path)"))
        if csv_path is None or not csv_path.strip():
            return
        try:
            text = Path(csv_path.strip()).read_text(encoding="utf-8-sig")
        except OSError as exc:
            self._set_message(f"Could not read CSV: {exc}")
            return
        events, issues = parse_csv(text)
        if not events:
            await self.push_screen_wait(
                ReportScreen("Calendar import", issues or ["No events found."]))
            return

        raw_code = await self.push_screen_wait(
            PromptScreen("Blackout status code (Out of Office)", "4"))
        if raw_code is None:
            return
        try:
            blackout_code = int(raw_code.strip())
        except ValueError:
            blackout_code = 4

        starts = [e.start.date() for e in events]
        start = await self._prompt_date("Import start")
        if start is _CANCEL:
            return
        if start is None:
            start = min(starts)
        end = await self._prompt_date("Import end")
        if end is _CANCEL:
            return
        if end is None:
            end = max(starts)
        if end < start:
            self._set_message("End date is before start date")
            return

        plan = plan_import(events, start, end, blackout_code)
        if not plan.candidates:
            await self.push_screen_wait(ReportScreen("Calendar import", [
                f"No import candidates in {start}..{end}.",
                f"({len(plan.ignored_blackout)} masked by blackout windows)"]
                + issues))
            return

        make_backup(self.doc.path)
        self.checkpoint()
        await self._run_import(plan, blackout_code)

    async def _run_import(self, plan, blackout_code: int) -> None:
        all_targets: dict = {}
        ignore_subjects: set = set()
        imported = skipped = ignored = dup = 0

        for event in reversed(plan.candidates):
            if event.subject in ignore_subjects:
                ignored += 1
                continue
            if self.doc.find_clock(event.start, event.end) is not None:
                dup += 1
                continue

            if event.subject in all_targets:
                project, task = all_targets[event.subject]
                if await self._import_event(event, project, task):
                    imported += 1
                else:
                    skipped += 1
                continue

            choice = await self.push_screen_wait(ImportChoiceScreen(event, blackout_code))
            if choice == "quit":
                break
            if choice == "skip":
                skipped += 1
                continue
            if choice == "ignore_all":
                ignore_subjects.add(event.subject)
                ignored += 1
                continue
            project = await self._choose_project()
            if project is None:
                skipped += 1
                continue
            task = await self._choose_task(project, default_name=event.subject)
            if task is None:
                skipped += 1
                continue
            if choice == "all":
                all_targets[event.subject] = (project, task)
            if await self._import_event(event, project, task):
                imported += 1
            else:
                skipped += 1

        self.save_and_refresh()
        self._set_message(f"Import done: {imported} added, {skipped} skipped, "
                          f"{ignored} ignored, {dup} duplicate(s)")

    async def _choose_project(self):
        projects = sorted(self.doc.projects, key=lambda p: p.name.lower())
        names = ["+ New project"] + [p.name for p in projects]
        idx = await self.push_screen_wait(ListChoiceScreen("Import into project", names, 0))
        if idx is None:
            return None
        if idx == 0:
            name = await self.push_screen_wait(PromptScreen("New project name"))
            if name is None or not name.strip():
                return None
            now = self._now()
            project = Project(name=name.strip(), created=now, modified=now)
            self.doc.projects.append(project)
            return project
        return projects[idx - 1]

    async def _choose_task(self, project, default_name: str):
        tasks = sorted(project.tasks, key=lambda t: t.name.lower())
        names = ["+ New task"] + [t.name for t in tasks]
        idx = await self.push_screen_wait(
            ListChoiceScreen(f"Task in {project.name}", names, 0))
        if idx is None:
            return None
        if idx == 0:
            name = await self.push_screen_wait(PromptScreen("New task name", default_name))
            if name is None or not name.strip():
                return None
            now = self._now()
            task = Task(name=name.strip(), created=now, modified=now)
            project.tasks.append(task)
            project.modified = now
            project.collapsed = False
            return task
        return tasks[idx - 1]

    async def _import_event(self, event, project, task) -> bool:
        """Write one calendar event as a clock on ``task``, resolving any
        overlap interactively.  Returns True if imported, False if skipped
        at the overlap popup."""
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

        choice = await self.push_screen_wait(PrecedenceScreen("Import overlap", lines))
        if choice is None:
            task.clocks.remove(clock)
            return False
        if choice == "e":
            apply_overlap_changes(changes)
            for c in changes:
                self.doc.touch(c.clock)
            self.doc.touch(clock)
        else:
            apply_reshape(self.doc, clock, pieces, event.start)
            self.doc.touch(clock)
        return True

    def action_reload(self) -> None:
        self._begin()
        self.checkpoint()
        self.doc, self.load_issues = load(self.doc.path)
        self.refresh_rows()
        msg = f"Reloaded {self.doc.path}"
        if self.load_issues:
            msg += f" ({len(self.load_issues)} problem(s); press v)"
        self._set_message(msg)

    def warn_about(self, clock: ClockEntry) -> None:
        warnings = clock_warnings(clock)
        if warnings:
            self._set_message("WARNING: clock " + "; ".join(warnings))

    @work
    async def action_show_help(self) -> None:
        await self.push_screen_wait(ReportScreen("Help", HELP_LINES))


# -- timeline mode -----------------------------------------------------------


class TimelineScreen(Screen):
    """Full-screen view of one day's workday with entries and gaps.

    Navigate rows; on a gap press 'a' (or Enter) to add an entry; on an
    entry press 'e' to edit its time or 'd' to delete it; '['/']' change
    day, 'g' jumps to a date, '<'/'>' widen the window earlier/later, 'R'
    resets the window, 'W' saves the current window as the new default,
    'u' undoes, 'q'/Esc returns.
    """

    CSS = """
    TimelineScreen { layout: vertical; }
    #tl-header, #tl-summary, #tl-footer { height: 1; }
    #tl-header, #tl-footer { background: $primary-darken-2; color: white; }
    OptionList { height: 1fr; }
    """

    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("left_square_bracket", "prev_day", show=False),
        Binding("right_square_bracket", "next_day", show=False),
        Binding("g", "goto_date", show=False),
        Binding("less_than_sign", "widen_earlier", show=False),
        Binding("greater_than_sign", "widen_later", show=False),
        Binding("R", "reset_window", show=False),
        Binding("W", "save_default", show=False),
        Binding("u", "undo", show=False),
        Binding("a", "add_in_gap", show=False),
        Binding("enter", "add_in_gap", show=False),
        Binding("e", "edit_entry", show=False),
        Binding("d", "delete_entry", show=False),
        Binding("q", "leave", show=False),
        Binding("escape", "leave", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.day = date.today()
        self.tl_rows: list = []

    def compose(self) -> ComposeResult:
        yield Static(id="tl-header")
        yield Static(id="tl-summary")
        yield OptionList(id="tl-list")
        yield Static(
            "a/Enter add   [ ] day   < > widen   R reset   W save default"
            "   g goto   u undo   q back",
            id="tl-footer")

    def on_mount(self) -> None:
        app = self.app
        self.start_hour = app.config.workday_start
        self.end_hour = app.config.workday_end
        self.query_one("#tl-list", OptionList).focus()
        self._refresh()

    def _refresh(self) -> None:
        app = self.app
        self.tl_rows, ws, we = timeline_rows(app.doc, self.day, self.start_hour, self.end_hour)
        hidden_before, hidden_after = timeline_hidden_counts(app.doc, self.day, ws, we)
        worked = sum((r.duration for r in self.tl_rows if r.kind == ENTRY), timedelta())
        free = sum((r.duration for r in self.tl_rows if r.kind == GAP), timedelta())
        header = f" Timeline — {self.day:%a %Y-%m-%d}   window {ws:%H:%M}-{we:%H:%M} "
        summary = (f" worked {human_duration(worked)}   free {human_duration(free)}"
                   f"   ({sum(1 for r in self.tl_rows if r.kind == GAP)} gap(s))")
        hints = []
        if hidden_before:
            hints.append(f"+{hidden_before} earlier (< to expand)")
        if hidden_after:
            hints.append(f"+{hidden_after} later (> to expand)")
        if hints:
            summary += "   " + ", ".join(hints)
        self.query_one("#tl-header", Static).update(header)
        self.query_one("#tl-summary", Static).update(
            Text(summary, style="bold red" if hints else ""))

        ol = self.query_one("#tl-list", OptionList)
        prev = ol.highlighted or 0
        ol.clear_options()
        for row in self.tl_rows:
            span = f"{row.start:%H:%M}-{row.end:%H:%M}"
            dur = human_duration(row.duration)
            if row.kind == GAP:
                text = Text(f"  {span}  {'─' * 6} GAP {dur} {'─' * 6}", style="bold red")
            else:
                label = f"{row.task.name} ({row.project.name})" if row.task else ""
                text = Text(f"  {span}  {label}   [{dur}]", style="yellow")
            ol.add_option(Option(text))
        if self.tl_rows:
            ol.highlighted = max(0, min(prev, len(self.tl_rows) - 1))

    def action_cursor_down(self) -> None:
        self.query_one("#tl-list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#tl-list", OptionList).action_cursor_up()

    def _current_row(self):
        ol = self.query_one("#tl-list", OptionList)
        idx = ol.highlighted
        if idx is not None and 0 <= idx < len(self.tl_rows):
            return self.tl_rows[idx]
        return None

    def action_prev_day(self) -> None:
        self.day -= timedelta(days=1)
        self._refresh()

    def action_next_day(self) -> None:
        self.day += timedelta(days=1)
        self._refresh()

    @work
    async def action_goto_date(self) -> None:
        when = await self.app._prompt_date("Go to date")
        if when not in (None, _CANCEL):
            self.day = when
            self._refresh()

    def action_widen_earlier(self) -> None:
        self.start_hour = max(0, self.start_hour - 1)
        self._refresh()

    def action_widen_later(self) -> None:
        self.end_hour = min(24, self.end_hour + 1)
        self._refresh()

    def action_reset_window(self) -> None:
        self.start_hour = self.app.config.workday_start
        self.end_hour = self.app.config.workday_end
        self._refresh()
        self.app._set_message("Window reset to default")

    def action_save_default(self) -> None:
        app = self.app
        app.config = Config(self.start_hour, self.end_hour, app.config.notes_dir)
        save_config(app.doc.path, app.config)
        app._set_message(f"Saved default window: "
                         f"{self.start_hour:02d}:00-{self.end_hour:02d}:00")

    def action_undo(self) -> None:
        self.app.action_undo()
        self._refresh()

    @work
    async def action_add_in_gap(self) -> None:
        row = self._current_row()
        if row is None or row.kind != GAP:
            self.app._set_message("Select a gap to add an entry")
            return
        app = self.app
        gs, ge = row.start, row.end
        s_raw = await self.app.push_screen_wait(
            PromptScreen(f"Entry start (in {gs:%H:%M}-{ge:%H:%M})", gs.strftime("%H:%M")))
        if s_raw is None:
            return
        start = parse_user_ts(s_raw, base=gs)
        e_raw = await self.app.push_screen_wait(PromptScreen("Entry end", ge.strftime("%H:%M")))
        if e_raw is None:
            return
        end = parse_user_ts(e_raw, base=gs)
        if start is None or end is None:
            app._set_message("Invalid time")
            return
        if not (gs <= start < end <= ge):
            app._set_message(f"Entry must be within the gap {gs:%H:%M}-{ge:%H:%M}")
            return
        project = await app._choose_project()
        if project is None:
            return
        task = await app._choose_task(project, default_name="")
        if task is None:
            return
        app.checkpoint()
        clock = ClockEntry(start=start, end=end)
        task.clocks.append(clock)
        app._reveal(task, clock)
        app.doc.touch(clock)
        app.doc.save()
        app._set_message(f"Added {start:%H:%M}-{end:%H:%M} to {task.name}")
        self._refresh()

    @work
    async def action_edit_entry(self) -> None:
        row = self._current_row()
        if row is None or row.kind != ENTRY:
            self.app._set_message("Select a time entry to edit")
            return
        await self.app.edit_clock(row.clock)
        self._refresh()

    @work
    async def action_delete_entry(self) -> None:
        row = self._current_row()
        if row is None or row.kind != ENTRY:
            self.app._set_message("Select a time entry to delete")
            return
        ok = await self.app.push_screen_wait(
            ConfirmScreen("Delete this clock entry (and its comments)?"))
        if not ok:
            return
        self.app.checkpoint()
        self.app._remove_clock(row.task, row.clock)
        self.app.doc.save()
        self.app._set_message("Deleted (kept as ## lines — press X to expunge, R to restore)")
        self._refresh()

    def action_leave(self) -> None:
        self.app.refresh_rows()
        self.app._set_message("Left timeline")
        self.app.pop_screen()


# -- priority mode ------------------------------------------------------------


class PriorityScreen(Screen):
    """Flat, cross-project triage view: every open task, sorted by task
    priority then project priority then most-recently-touched first (see
    ``priority_rows``).

    Navigation and status(s/S)/done(D)/priority(1-5) act directly here and
    re-sort or drop the row immediately, same as the normal view. 'i'
    clocks the selected task in and drops straight back to the normal
    view, cursor on the new clock entry. 'q'/Esc returns without clocking
    in.
    """

    CSS = """
    PriorityScreen { layout: vertical; }
    OptionList { height: 1fr; }
    #pr-footer { height: 1; }
    """

    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("g", "first", show=False),
        Binding("G", "last", show=False),
        Binding("s", "cycle_status(1)", show=False),
        Binding("S", "cycle_status(-1)", show=False),
        Binding("D", "mark_done", show=False),
        Binding("1", "set_priority(1)", show=False),
        Binding("2", "set_priority(2)", show=False),
        Binding("3", "set_priority(3)", show=False),
        Binding("4", "set_priority(4)", show=False),
        Binding("5", "set_priority(5)", show=False),
        Binding("i", "clock_in_and_return", show=False),
        Binding("q", "leave", show=False),
        Binding("escape", "leave", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.pr_rows: list = []

    def compose(self) -> ComposeResult:
        yield OptionList(id="pr-list")
        yield Static(
            "PRIORITY VIEW  i: clock in & return   s/S: status   "
            "1-5: priority   D: done   q: back",
            id="pr-footer")

    def on_mount(self) -> None:
        self.query_one("#pr-list", OptionList).focus()
        self._refresh()

    def _refresh(self) -> None:
        app = self.app
        prev = self._selected_task()
        self.pr_rows = priority_rows(app.doc, app._now())
        ol = self.query_one("#pr-list", OptionList)
        ol.clear_options()
        for row in self.pr_rows:
            ol.add_option(Option(Text(row.text, style=_row_style(row))))
        if not self.pr_rows:
            app._begin()
            app._set_message("No open tasks remain")
            self.app.pop_screen()
            return
        cursor = 0
        if prev is not None:
            for i, row in enumerate(self.pr_rows):
                if row.obj is prev:
                    cursor = i
                    break
        ol.highlighted = max(0, min(cursor, len(self.pr_rows) - 1))

    def action_cursor_down(self) -> None:
        self.query_one("#pr-list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#pr-list", OptionList).action_cursor_up()

    def action_first(self) -> None:
        self.query_one("#pr-list", OptionList).action_first()

    def action_last(self) -> None:
        self.query_one("#pr-list", OptionList).action_last()

    def _selected_task(self) -> Task | None:
        ol = self.query_one("#pr-list", OptionList)
        idx = ol.highlighted
        if idx is not None and 0 <= idx < len(self.pr_rows):
            return self.pr_rows[idx].obj
        return None

    def action_cycle_status(self, step: int = 1) -> None:
        task = self._selected_task()
        if task is None:
            return
        app = self.app
        app.checkpoint()
        app._set_status(
            task, STATUSES[(STATUSES.index(task.status) + step) % len(STATUSES)])
        app.doc.touch(task)
        app.doc.save()
        self._refresh()

    def action_mark_done(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        app = self.app
        app.checkpoint()
        app._set_status(task, "DONE")
        app.doc.touch(task)
        app.doc.save()
        self._refresh()

    def action_set_priority(self, value: int) -> None:
        task = self._selected_task()
        if task is None:
            return
        app = self.app
        app.checkpoint()
        task.priority = value
        app.doc.touch(task)
        app.doc.save()
        self._refresh()

    async def action_clock_in_and_return(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        app = self.app
        project = app.doc.project_of(task)
        if project is not None:
            project.collapsed = False
        self.app.pop_screen()
        app.refresh_rows()
        app._select_obj(task)
        await app.action_clock_in()

    def action_leave(self) -> None:
        self.app.refresh_rows()
        self.app.pop_screen()


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("timelog.org")
    path = path.resolve()
    OrgtimeApp(path).run()


if __name__ == "__main__":
    main()
