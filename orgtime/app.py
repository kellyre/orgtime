"""Textual TUI for orgtime."""

from __future__ import annotations

import copy
from datetime import datetime
from pathlib import Path

from rich.markup import escape
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Static,
    TextArea,
    Tree,
)

from .model import (
    CLOSED_STATUSES,
    STATUSES,
    TS_FORMAT,
    ClockEntry,
    Document,
    Project,
    Task,
    check_consistency,
    clock_warnings,
    comment_lines,
    format_duration,
    format_ts,
    human_duration,
    load,
    parse_user_date,
    parse_user_ts,
    tombstoned,
)
from .report import build_report, default_filename
from .view import COMMENT, next_match_index, search_targets

STATUS_COLORS = {
    "TODO": "red",
    "IN-PROGRESS": "yellow",
    "HOLD": "magenta",
    "CANCELLED": "dim",
    "DONE": "green",
}

PRIORITY_COLORS = {1: "bold red", 2: "orange1", 3: "yellow", 4: "cyan", 5: "dim"}

UNDO_LIMIT = 100


def project_label(project: Project, now: datetime) -> str:
    total = project.total_time(now)
    time_part = f"  [dim]{human_duration(total)}[/]" if total else ""
    return (
        f"[{PRIORITY_COLORS[project.priority]}]#{project.priority}[/] "
        f"[bold]{escape(project.name)}[/]"
        f"  [dim]({len(project.tasks)} tasks)[/]{time_part}"
    )


def task_label(task: Task, now: datetime) -> str:
    total = task.total_time(now)
    time_part = f"  [dim]{human_duration(total)}[/]" if total else ""
    running = "  [blink bold yellow]⏱[/]" if task.running_clock() else ""
    return (
        f"[{STATUS_COLORS.get(task.status, 'white')}]{task.status}[/] "
        f"[{PRIORITY_COLORS[task.priority]}]#{task.priority}[/] "
        f"{escape(task.name)}{time_part}{running}"
    )


class CommentRef:
    """Tree-node marker: a comment block belonging to a project/task/clock.

    All lines of one block share a single CommentRef, so edit/delete on any
    line acts on the whole adjoining block.
    """

    def __init__(self, owner: Project | Task | ClockEntry) -> None:
        self.owner = owner


def comment_line_label(text: str) -> str:
    return f"[italic dim green]# {escape(text)}[/]" if text else "[italic dim green]#[/]"


def clock_label(clock: ClockEntry, now: datetime) -> str:
    warn = "  [bold red]⚠[/]" if clock_warnings(clock, now) else ""
    if clock.running:
        return (
            f"[dim]CLOCK:[/] {format_ts(clock.start)}--... "
            f"[bold yellow]running {human_duration(clock.duration(now))}[/]{warn}"
        )
    return (
        f"[dim]CLOCK:[/] {format_ts(clock.start)}--{format_ts(clock.end)} "
        f"[dim]=>[/] {format_duration(clock.duration())}{warn}"
    )


class UndoInput(Input):
    """Input that doesn't select-all on focus and supports ctrl+z text undo.

    Plain Input highlights its whole value when focused, so a stray keystroke
    silently wipes the text.  Here the cursor just goes to the end, and every
    change is kept in a small history that ctrl+z steps back through.
    """

    BINDINGS = [Binding("ctrl+z", "text_undo", "Undo text", show=False)]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, select_on_focus=False, **kwargs)
        self._history: list[str] = [self.value]

    def on_focus(self, event) -> None:
        self.cursor_position = len(self.value)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.value != self._history[-1]:
            self._history.append(event.value)
            del self._history[:-100]

    def action_text_undo(self) -> None:
        if len(self._history) > 1:
            self._history.pop()
            self.value = self._history[-1]
        self.cursor_position = len(self.value)


class EditDialog(ModalScreen[dict | None]):
    """Create/edit a project or a task.

    Enter on the name saves immediately; ctrl+s saves from any field.
    Priority and status are radio rows (arrow keys), not dropdowns.
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, title: str, *, name: str = "", description: str = "",
                 priority: int = 3, status: str | None = None) -> None:
        super().__init__()
        self._title = title
        self._name = name
        self._description = description
        self._priority = priority
        self._status = status  # None => project (no status field)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="dialog-title")
            yield Label("Name")
            yield UndoInput(value=self._name, id="name")
            yield Label("Description")
            yield TextArea(self._description, id="description")
            yield Label("Priority (1 = highest)")
            with RadioSet(id="priority"):
                for p in range(1, 6):
                    yield RadioButton(str(p), value=p == self._priority)
            if self._status is not None:
                yield Label("Status")
                with RadioSet(id="status"):
                    for s in STATUSES:
                        yield RadioButton(s, value=s == self._status)
            yield Label("[dim]enter or ctrl+s saves · esc cancels[/]", id="dialog-hint")
            with Horizontal(id="dialog-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#name", Input).focus()

    def action_save(self) -> None:
        self._save()

    @on(Input.Submitted)
    @on(Button.Pressed, "#save")
    def save_event(self, event) -> None:
        self._save()

    def _save(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self.notify("Name must not be empty", severity="error")
            return
        result = {
            "name": name,
            "description": self.query_one("#description", TextArea).text.strip(),
            "priority": self.query_one("#priority", RadioSet).pressed_index + 1,
        }
        if self._status is not None:
            result["status"] = STATUSES[self.query_one("#status", RadioSet).pressed_index]
        self.dismiss(result)

    @on(Button.Pressed, "#cancel")
    def cancel(self, event) -> None:
        self.dismiss(None)


class CommentDialog(ModalScreen[str | None]):
    """Free-form multi-line comment editor for one comment block."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, title: str, text: str = "") -> None:
        super().__init__()
        self._title = title
        self._text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="dialog-title")
            yield TextArea(self._text, id="comment-text")
            yield Label("[dim]ctrl+s saves · esc cancels · empty text removes "
                        "the comment[/]", id="dialog-hint")
            with Horizontal(id="dialog-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        area = self.query_one("#comment-text", TextArea)
        area.focus()
        area.cursor_location = area.document.end

    def action_save(self) -> None:
        self.dismiss(self.query_one("#comment-text", TextArea).text)

    @on(Button.Pressed, "#save")
    def save_event(self, event) -> None:
        self.action_save()

    @on(Button.Pressed, "#cancel")
    def cancel(self, event) -> None:
        self.dismiss(None)


class TimeDialog(ModalScreen[datetime | None]):
    """Ask for a single timestamp (used by clock-in-at / clock-out-at)."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, *, base: datetime | None = None) -> None:
        super().__init__()
        self._title = title
        self._base = base  # date used when the user types a bare HH:MM

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="dialog-title")
            yield UndoInput(value=datetime.now().strftime(TS_FORMAT), id="when")
            yield Label("[dim]YYYY-MM-DD HH:MM, or just HH:MM · enter saves[/]",
                        id="dialog-hint")
            with Horizontal(id="dialog-buttons"):
                yield Button("OK", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    @on(Input.Submitted)
    @on(Button.Pressed, "#save")
    def save(self, event) -> None:
        when = parse_user_ts(self.query_one("#when", Input).value, self._base)
        if when is None:
            self.notify("Invalid time — use YYYY-MM-DD HH:MM or HH:MM",
                        severity="error")
            return
        self.dismiss(when)

    @on(Button.Pressed, "#cancel")
    def cancel(self, event) -> None:
        self.dismiss(None)


class ClockDialog(ModalScreen[dict | None]):
    """Edit an existing clock entry's start/end timestamps."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, clock: ClockEntry) -> None:
        super().__init__()
        self._clock = clock

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Edit clock entry", id="dialog-title")
            yield Label("Start")
            yield UndoInput(value=self._clock.start.strftime(TS_FORMAT), id="start")
            yield Label("End (leave empty for a running clock)")
            end_value = self._clock.end.strftime(TS_FORMAT) if self._clock.end else ""
            yield UndoInput(value=end_value, id="end")
            yield Label("[dim]YYYY-MM-DD HH:MM, or just HH:MM · enter saves[/]",
                        id="dialog-hint")
            with Horizontal(id="dialog-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def action_save(self) -> None:
        self._save()

    @on(Input.Submitted)
    @on(Button.Pressed, "#save")
    def save_event(self, event) -> None:
        self._save()

    def _save(self) -> None:
        start = parse_user_ts(self.query_one("#start", Input).value)
        if start is None:
            self.notify("Invalid start timestamp", severity="error")
            return
        end_raw = self.query_one("#end", Input).value.strip()
        end = None
        if end_raw:
            end = parse_user_ts(end_raw, base=start)
            if end is None:
                self.notify("Invalid end timestamp", severity="error")
                return
            if end < start:
                self.notify("End is before start", severity="error")
                return
        self.dismiss({"start": start, "end": end})

    @on(Button.Pressed, "#cancel")
    def cancel(self, event) -> None:
        self.dismiss(None)


class ConfirmDialog(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "dismiss(False)", "Cancel"),
        Binding("y", "dismiss(True)", "Yes"),
        Binding("n", "dismiss(False)", "No"),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._message, id="dialog-title")
            with Horizontal(id="dialog-buttons"):
                yield Button("Yes (y)", variant="error", id="yes")
                yield Button("No (n)", id="no")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class ReportDialog(ModalScreen[None]):
    """Show a list of consistency problems (or parse issues)."""

    BINDINGS = [Binding("escape,q,enter", "dismiss(None)", "Close")]

    def __init__(self, title: str, lines: list[str]) -> None:
        super().__init__()
        self._title = title
        self._lines = lines

    def compose(self) -> ComposeResult:
        with Vertical(id="report"):
            yield Label(self._title, id="dialog-title")
            with VerticalScroll():
                if self._lines:
                    for line in self._lines:
                        yield Static(f"[red]•[/] {escape(line)}")
                else:
                    yield Static("[green]No problems found.[/]")
            yield Button("Close (esc)", id="close")

    @on(Button.Pressed)
    def pressed(self, event) -> None:
        self.dismiss(None)


class ReportInputDialog(ModalScreen[dict | None]):
    """Collect a date range and filename for a time report."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel"),
        Binding("ctrl+s", "save", "Generate"),
    ]

    def __init__(self, doc: Document) -> None:
        super().__init__()
        self._doc = doc

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Generate time report", id="dialog-title")
            yield Label("Start date (YYYY-MM-DD, blank = open-ended)")
            yield UndoInput(id="start")
            yield Label("End date (YYYY-MM-DD, blank = open-ended)")
            yield UndoInput(id="end")
            yield Label("Write to file")
            yield UndoInput(value=default_filename(self._doc, None, None),
                            id="path")
            yield Label("[dim]enter or ctrl+s generates · esc cancels[/]",
                        id="dialog-hint")
            with Horizontal(id="dialog-buttons"):
                yield Button("Generate", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#start", Input).focus()

    def _date(self, field: str):
        raw = self.query_one(f"#{field}", Input).value.strip()
        if not raw:
            return None, True
        d = parse_user_date(raw)
        return d, d is not None

    def action_save(self) -> None:
        self._save()

    @on(Input.Submitted)
    @on(Button.Pressed, "#save")
    def save_event(self, event) -> None:
        self._save()

    def _save(self) -> None:
        start, ok1 = self._date("start")
        end, ok2 = self._date("end")
        if not ok1 or not ok2:
            self.notify("Invalid date — use YYYY-MM-DD", severity="error")
            return
        if start is not None and end is not None and end < start:
            self.notify("End date is before start date", severity="error")
            return
        name = self.query_one("#path", Input).value.strip() \
            or default_filename(self._doc, start, end)
        self.dismiss({"start": start, "end": end, "name": name})

    @on(Button.Pressed, "#cancel")
    def cancel(self, event) -> None:
        self.dismiss(None)


class SearchDialog(ModalScreen[str | None]):
    """Single-field substring search prompt."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, initial: str = "") -> None:
        super().__init__()
        self._initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Search (substring)", id="dialog-title")
            yield UndoInput(value=self._initial, id="term")
            yield Label("[dim]enter: jump to next match · esc: cancel[/]",
                        id="dialog-hint")
            with Horizontal(id="dialog-buttons"):
                yield Button("Search", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#term", Input).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def ok(self, event) -> None:
        self.dismiss(self.query_one("#term", Input).value)

    @on(Button.Pressed, "#cancel")
    def cancel(self, event) -> None:
        self.dismiss(None)


class MoveTaskDialog(ModalScreen[int | None]):
    """Pick a destination project for the selected task."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel"),
        Binding("ctrl+s", "save", "Move"),
    ]

    def __init__(self, task_name: str, project_names: list[str]) -> None:
        super().__init__()
        self._task_name = task_name
        self._names = project_names

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Move '{self._task_name}' to project", id="dialog-title")
            with RadioSet(id="dest"):
                for i, name in enumerate(self._names):
                    yield RadioButton(name, value=i == 0)
            yield Label("[dim]arrows choose · ctrl+s: move · esc: cancel[/]",
                        id="dialog-hint")
            with Horizontal(id="dialog-buttons"):
                yield Button("Move", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#dest", RadioSet).focus()

    def action_save(self) -> None:
        self._save()

    @on(Button.Pressed, "#ok")
    def ok_btn(self, event) -> None:
        self._save()

    def _save(self) -> None:
        index = self.query_one("#dest", RadioSet).pressed_index
        self.dismiss(max(0, index))

    @on(Button.Pressed, "#cancel")
    def cancel(self, event) -> None:
        self.dismiss(None)


class OrgTimeApp(App):
    TITLE = "orgtime"

    CSS = """
    #dialog, #report {
        width: 64;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }
    #report { width: 90; }
    #dialog-title { text-style: bold; margin-bottom: 1; }
    #dialog-hint { margin-top: 1; }
    #dialog-buttons { height: auto; margin-top: 1; align-horizontal: right; }
    #dialog-buttons Button { margin-left: 2; }
    #dialog TextArea { height: 3; }
    #dialog #comment-text { height: 8; }
    #dialog RadioSet { layout: horizontal; height: auto; width: 100%; }
    #dialog #dest { layout: vertical; max-height: 12; }
    EditDialog, TimeDialog, ClockDialog, ConfirmDialog, ReportDialog,
    CommentDialog {
        align: center middle;
    }
    #clockbar {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text;
    }
    Tree { padding: 0 1; }
    """

    BINDINGS = [
        Binding("N", "new_project", "New proj"),
        Binding("n", "new_task", "New task"),
        Binding("e", "edit", "Edit"),
        Binding("d", "delete", "Delete"),
        Binding("i", "clock_in", "In"),
        Binding("o", "clock_out", "Out"),
        Binding("I", "clock_in_at", "In at…"),
        Binding("O", "clock_out_at", "Out at…"),
        Binding("t", "cycle_status", "Status"),
        Binding("T", "cycle_status_back", "Status back", show=False),
        Binding("D", "mark_done", "Done"),
        Binding("m", "comment", "Comment"),
        Binding("X", "expunge", "Expunge", show=False),
        Binding("ctrl+z", "undo", "Undo"),
        Binding("u", "undo", "Undo", show=False),
        Binding("ctrl+y", "redo", "Redo", show=False),
        Binding("c", "check", "Check"),
        Binding("R", "report", "Report"),
        Binding("J", "jump_running", "Running"),
        Binding("C", "collapse_all", "Collapse all"),
        Binding("/", "search", "Search"),
        Binding("M", "move_task", "Move"),
        Binding("r", "reload", "Reload", show=False),
        Binding("q", "quit", "Quit"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        *[Binding(str(p), f"priority({p})", f"Priority {p}", show=False)
          for p in range(1, 6)],
    ]

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.doc = Document(path=path)
        self._load_issues: list[str] = []
        self._undo: list[Document] = []
        self._redo: list[Document] = []
        self._search_term = ""

    # -- lifecycle ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        tree: Tree = Tree("Projects", id="tree")
        tree.show_root = False
        tree.guide_depth = 3
        yield tree
        yield Static("", id="clockbar")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = str(self.doc.path)
        self.doc, self._load_issues = load(self.doc.path)
        self.rebuild_tree()
        self.update_clockbar()
        self.set_interval(1.0, self.tick)
        if self._load_issues:
            self.push_screen(ReportDialog("Problems while loading file", self._load_issues))

    # -- tree --------------------------------------------------------------

    def rebuild_tree(self) -> None:
        tree = self.query_one("#tree", Tree)
        selected = tree.cursor_node.data if tree.cursor_node else None

        tree.clear()
        now = datetime.now()
        select_line: int | None = None

        def consider(data, node_line: int) -> None:
            nonlocal select_line
            if select_line is not None:
                return
            if data is selected or (
                isinstance(selected, CommentRef) and isinstance(data, CommentRef)
                and data.owner is selected.owner
            ):
                select_line = node_line

        def add_comments(node, owner) -> None:
            ref = CommentRef(owner)
            for text in owner.comments:
                leaf = node.add_leaf(comment_line_label(text), data=ref)
                consider(ref, leaf.line)

        for project in self.doc.projects:
            pnode = tree.root.add(
                project_label(project, now), data=project, expand=not project.collapsed
            )
            consider(project, pnode.line)
            add_comments(pnode, project)
            for task in project.tasks:
                tnode = pnode.add(
                    task_label(task, now), data=task, expand=not task.collapsed
                )
                consider(task, tnode.line)
                add_comments(tnode, task)
                for clock in task.clocks:
                    if clock.comments:
                        cnode = tnode.add(clock_label(clock, now), data=clock,
                                          expand=True)
                        add_comments(cnode, clock)
                    else:
                        cnode = tnode.add_leaf(clock_label(clock, now), data=clock)
                    consider(clock, cnode.line)
        tree.root.expand()
        if select_line is not None and select_line >= 0:
            tree.cursor_line = select_line
        elif self.doc.projects and (tree.cursor_node is None or tree.cursor_node.data is None):
            tree.cursor_line = 0

    def refresh_labels(self) -> None:
        now = datetime.now()

        def walk(node) -> None:
            for child in node.children:
                data = child.data
                if isinstance(data, Project):
                    child.set_label(project_label(data, now))
                elif isinstance(data, Task):
                    child.set_label(task_label(data, now))
                elif isinstance(data, ClockEntry):
                    child.set_label(clock_label(data, now))
                walk(child)

        walk(self.query_one("#tree", Tree).root)

    def tick(self) -> None:
        self.update_clockbar()
        if self.doc.running():
            self.refresh_labels()

    def update_clockbar(self) -> None:
        bar = self.query_one("#clockbar", Static)
        active = self.doc.running()
        if active:
            project, task, clock = active
            bar.update(
                f"[bold yellow]⏱ {human_duration(clock.duration())}[/] "
                f"on [bold]{escape(task.name)}[/] [dim]({escape(project.name)}, "
                f"since {clock.start.strftime('%H:%M')})[/]"
            )
        else:
            bar.update("[dim]no clock running[/]")

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        if isinstance(event.node.data, (Project, Task)):
            event.node.data.collapsed = False

    def on_tree_node_collapsed(self, event: Tree.NodeCollapsed) -> None:
        if isinstance(event.node.data, (Project, Task)):
            event.node.data.collapsed = True

    # -- helpers -----------------------------------------------------------

    def selected(self):
        node = self.query_one("#tree", Tree).cursor_node
        return node.data if node else None

    def selected_item(self):
        """Selected object with CommentRef unwrapped to its owner."""
        obj = self.selected()
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
        self.rebuild_tree()
        self.update_clockbar()

    def warn_about(self, clock: ClockEntry) -> None:
        """Toast plausibility warnings for a just-closed/edited clock entry."""
        for warning in clock_warnings(clock):
            self.notify(f"Clock entry {warning}", severity="warning", timeout=8)

    # -- undo --------------------------------------------------------------

    def checkpoint(self) -> None:
        """Snapshot the document before a change, for undo."""
        self._undo.append(copy.deepcopy(self.doc))
        del self._undo[:-UNDO_LIMIT]
        self._redo.clear()

    def _restore(self, doc: Document) -> None:
        self.doc = doc
        self.doc.save()
        self.rebuild_tree()
        self.update_clockbar()

    def action_undo(self) -> None:
        if not self._undo:
            self.notify("Nothing to undo", severity="warning")
            return
        self._redo.append(self.doc)
        self._restore(self._undo.pop())
        self.notify(f"Undone ({len(self._undo)} more, ctrl+y to redo)")

    def action_redo(self) -> None:
        if not self._redo:
            self.notify("Nothing to redo", severity="warning")
            return
        self._undo.append(self.doc)
        self._restore(self._redo.pop())
        self.notify("Redone")

    # -- actions -----------------------------------------------------------

    def action_cursor_down(self) -> None:
        self.query_one("#tree", Tree).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#tree", Tree).action_cursor_up()

    def action_new_project(self) -> None:
        def done(result: dict | None) -> None:
            if result:
                self.checkpoint()
                self.doc.projects.append(Project(**result))
                self.save_and_refresh()
        self.push_screen(EditDialog("New project"), done)

    def action_new_task(self) -> None:
        obj = self.selected_item()
        if isinstance(obj, Project):
            project = obj
        elif (task := self.selected_task()) is not None:
            project = self.doc.project_of(task)
        else:
            self.notify("Select a project first", severity="warning")
            return

        def done(result: dict | None) -> None:
            if result:
                self.checkpoint()
                project.tasks.append(Task(**result))
                project.collapsed = False
                self.save_and_refresh()
        self.push_screen(EditDialog(f"New task in {project.name}", status="TODO"), done)

    def action_edit(self) -> None:
        obj = self.selected()
        if isinstance(obj, CommentRef):
            self._open_comment_dialog(obj.owner)
        elif isinstance(obj, ClockEntry):
            def done_clock(result: dict | None) -> None:
                if result:
                    self.checkpoint()
                    obj.start, obj.end = result["start"], result["end"]
                    self.save_and_refresh()
                    self.warn_about(obj)
            self.push_screen(ClockDialog(obj), done_clock)
        elif isinstance(obj, (Project, Task)):
            def done(result: dict | None) -> None:
                if result:
                    self.checkpoint()
                    for key, value in result.items():
                        setattr(obj, key, value)
                    self.save_and_refresh()
            kind = "task" if isinstance(obj, Task) else "project"
            self.push_screen(
                EditDialog(
                    f"Edit {kind}",
                    name=obj.name,
                    description=obj.description,
                    priority=obj.priority,
                    status=obj.status if isinstance(obj, Task) else None,
                ),
                done,
            )
        else:
            self.notify("Nothing selected", severity="warning")

    def action_delete(self) -> None:
        obj = self.selected()
        if obj is None:
            self.notify("Nothing selected", severity="warning")
            return
        if isinstance(obj, Project):
            message = f"Delete project '{obj.name}' and its {len(obj.tasks)} task(s)?"
        elif isinstance(obj, Task):
            message = f"Delete task '{obj.name}' and its clock entries?"
        elif isinstance(obj, CommentRef):
            message = "Delete this comment block?"
        else:
            message = "Delete this clock entry (and its comments)?"

        def done(confirmed: bool) -> None:
            if not confirmed:
                return
            self.checkpoint()
            # soft delete: lines are kept in the file with ## prepended
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
            self.notify("Deleted (kept as ## lines in the file — X expunges)")
        self.push_screen(ConfirmDialog(message), done)

    def action_clock_in(self) -> None:
        task = self.selected_task()
        if task is None:
            self.notify("Select a task to clock in", severity="warning")
            return
        self.checkpoint()
        self.doc.clock_in(task)
        task.collapsed = False
        self.save_and_refresh()
        self.notify(f"Clocked in: {task.name}")

    def action_clock_out(self) -> None:
        active = self.doc.running()
        if active is None:
            self.notify("No clock is running", severity="warning")
            return
        _, _, clock = active
        self.checkpoint()
        task = self.doc.clock_out()
        self.save_and_refresh()
        self.notify(f"Clocked out: {task.name}")
        self.warn_about(clock)

    def action_clock_in_at(self) -> None:
        task = self.selected_task()
        if task is None:
            self.notify("Select a task to clock in", severity="warning")
            return

        def done(when: datetime | None) -> None:
            if when is None:
                return
            self.checkpoint()
            self.doc.clock_in(task, when)
            task.collapsed = False
            self.save_and_refresh()
            self.notify(f"Clocked in: {task.name} at {when:%H:%M}")
            self.warn_about(task.clocks[-1])
        self.push_screen(TimeDialog(f"Clock in '{task.name}' at…"), done)

    def action_clock_out_at(self) -> None:
        active = self.doc.running()
        if active is None:
            self.notify("No clock is running", severity="warning")
            return
        _, task, clock = active

        def done(when: datetime | None) -> None:
            if when is None:
                return
            if when < clock.start:
                self.notify(
                    f"End is before the clock start ({format_ts(clock.start)})",
                    severity="error",
                )
                return
            self.checkpoint()
            clock.end = when
            self.save_and_refresh()
            self.notify(f"Clocked out: {task.name} at {when:%H:%M}")
            self.warn_about(clock)
        # bare HH:MM is interpreted on the day the clock started
        self.push_screen(
            TimeDialog(f"Clock out of '{task.name}' at…", base=clock.start), done
        )

    def _open_comment_dialog(self, owner) -> None:
        if isinstance(owner, Project):
            what = f"project '{owner.name}'"
        elif isinstance(owner, Task):
            what = f"task '{owner.name}'"
        else:
            what = f"clock entry {format_ts(owner.start)}"

        def done(text: str | None) -> None:
            if text is None:
                return
            lines = [line.rstrip() for line in text.splitlines()]
            while lines and not lines[0]:
                lines.pop(0)
            while lines and not lines[-1]:
                lines.pop()
            self.checkpoint()
            owner.comments = lines
            # make sure the new comment is visible
            if isinstance(owner, (Project, Task)):
                owner.collapsed = False
            if isinstance(owner, Task):
                self.doc.project_of(owner).collapsed = False
            elif isinstance(owner, ClockEntry):
                task = self.doc.task_of(owner)
                task.collapsed = False
                self.doc.project_of(task).collapsed = False
            self.save_and_refresh()
        self.push_screen(
            CommentDialog(f"Comment on {what}", "\n".join(owner.comments)), done
        )

    def action_comment(self) -> None:
        owner = self.selected_item()
        if owner is None:
            self.notify("Select a line to comment on", severity="warning")
            return
        self._open_comment_dialog(owner)

    def action_expunge(self) -> None:
        count = self.doc.tombstone_count()
        if count == 0:
            self.notify("No deleted lines to expunge")
            return

        def done(confirmed: bool) -> None:
            if not confirmed:
                return
            self.checkpoint()
            removed = self.doc.expunge()
            self.save_and_refresh()
            self.notify(f"Expunged {removed} deleted line(s)")
        self.push_screen(
            ConfirmDialog(f"Permanently remove {count} deleted (##) line(s)?"), done
        )

    def _apply_status(self, task: Task, status: str) -> None:
        """Set a task's status, closing its running clock if now closed."""
        task.status = status
        if status in CLOSED_STATUSES and task.running_clock():
            self.doc.clock_out()

    def action_cycle_status(self) -> None:
        self._cycle_status(1)

    def action_cycle_status_back(self) -> None:
        self._cycle_status(-1)

    def _cycle_status(self, step: int) -> None:
        task = self.selected_task()
        if task is None:
            self.notify("Select a task to change its status", severity="warning")
            return
        self.checkpoint()
        self._apply_status(
            task, STATUSES[(STATUSES.index(task.status) + step) % len(STATUSES)])
        self.save_and_refresh()

    def action_mark_done(self) -> None:
        obj = self.selected_item()
        if isinstance(obj, Project):
            n = len(obj.tasks)
            if n == 0:
                self.notify("Project has no tasks", severity="warning")
                return

            def done(confirmed: bool) -> None:
                if not confirmed:
                    return
                self.checkpoint()
                for task in obj.tasks:
                    self._apply_status(task, "DONE")
                self.save_and_refresh()
                self.notify(f"Marked {n} task(s) DONE in {obj.name}")
            self.push_screen(
                ConfirmDialog(f"Mark all {n} task(s) in '{obj.name}' as DONE?"),
                done,
            )
            return
        task = self.selected_task()
        if task is None:
            self.notify("Select a task or project to mark DONE", severity="warning")
            return
        self.checkpoint()
        self._apply_status(task, "DONE")
        self.save_and_refresh()
        self.notify(f"Marked DONE: {task.name}")

    def action_priority(self, value: int) -> None:
        obj = self.selected_item()
        if isinstance(obj, ClockEntry):
            obj = self.doc.task_of(obj)
        if obj is None:
            self.notify("Select a project or task", severity="warning")
            return
        self.checkpoint()
        obj.priority = value
        self.save_and_refresh()

    def action_check(self) -> None:
        problems = self._load_issues + check_consistency(self.doc)
        self.push_screen(ReportDialog("Consistency check", problems))

    def _focus_object(self, obj) -> None:
        # scan displayed lines (forces the tree's line cache to refresh after
        # a rebuild) rather than reading node.line, which can be stale
        tree = self.query_one("#tree", Tree)
        for line in range(tree.last_line + 1):
            node = tree.get_node_at_line(line)
            if node is not None and node.data is obj:
                tree.cursor_line = line
                return

    def action_jump_running(self) -> None:
        active = self.doc.running()
        if active is None:
            self.notify("No clock is running", severity="warning")
            return
        project, task, clock = active
        project.collapsed = False
        task.collapsed = False
        self.rebuild_tree()
        self._focus_object(clock)
        self.notify(f"Jumped to running clock on {task.name}")

    def _focus_comment(self, owner) -> None:
        tree = self.query_one("#tree", Tree)
        for line in range(tree.last_line + 1):
            node = tree.get_node_at_line(line)
            if (node is not None and isinstance(node.data, CommentRef)
                    and node.data.owner is owner):
                tree.cursor_line = line
                return

    def _current_target_index(self, targets) -> int:
        obj = self.selected()
        owner = obj.owner if isinstance(obj, CommentRef) else obj
        for i, target in enumerate(targets):
            if target.owner is owner:
                return i
        return -1

    def _reveal_and_select(self, target) -> None:
        target.project.collapsed = False
        if target.kind == COMMENT and target.task is not None:
            target.task.collapsed = False
        self.rebuild_tree()
        if target.kind == COMMENT:
            self._focus_comment(target.owner)
        else:
            self._focus_object(target.owner)

    def action_search(self) -> None:
        def done(term: str | None) -> None:
            if term is None:
                return
            term = term.strip()
            if not term:
                return
            self._search_term = term
            targets = search_targets(self.doc)
            if not targets:
                self.notify("Nothing to search")
                return
            idx = next_match_index(targets, term,
                                   self._current_target_index(targets))
            if idx is None:
                self.notify(f"No match for {term!r}", severity="warning")
                return
            self._reveal_and_select(targets[idx])
            self.notify(f"Search: {term}  (/ to repeat)")
        self.push_screen(SearchDialog(self._search_term), done)

    def action_move_task(self) -> None:
        task = self.selected_task()
        if task is None:
            self.notify("Select a task to move", severity="warning")
            return
        src = self.doc.project_of(task)
        others = [p for p in self.doc.projects if p is not src]
        if not others:
            self.notify("No other project to move to", severity="warning")
            return

        def done(index: int | None) -> None:
            if index is None:
                return
            dest = others[index]
            self.checkpoint()
            src.tasks.remove(task)
            dest.tasks.append(task)
            dest.collapsed = False
            self.save_and_refresh()
            self._focus_object(task)
            self.notify(f"Moved '{task.name}' to {dest.name}")
        self.push_screen(
            MoveTaskDialog(task.name, [p.name for p in others]), done)

    def action_collapse_all(self) -> None:
        obj = self.selected_item()
        project = None
        if isinstance(obj, Project):
            project = obj
        elif isinstance(obj, Task):
            project = self.doc.project_of(obj)
        elif isinstance(obj, ClockEntry):
            task = self.doc.task_of(obj)
            project = self.doc.project_of(task) if task else None
        for p in self.doc.projects:
            p.collapsed = True
        self.rebuild_tree()
        if project is not None:
            self._focus_object(project)
        self.notify("Collapsed all projects")

    def action_report(self) -> None:
        def done(result: dict | None) -> None:
            if not result:
                return
            out_path = Path(result["name"])
            if not out_path.is_absolute():
                base = self.doc.path.parent if self.doc.path else Path.cwd()
                out_path = base / out_path
            try:
                out_path.write_text(
                    build_report(self.doc, result["start"], result["end"]),
                    encoding="utf-8")
            except OSError as exc:
                self.notify(f"Could not write report: {exc}", severity="error")
                return
            self.notify(f"Wrote report to {out_path}")
        self.push_screen(ReportInputDialog(self.doc), done)

    def action_reload(self) -> None:
        self.checkpoint()
        self.doc, self._load_issues = load(self.doc.path)
        self.rebuild_tree()
        self.update_clockbar()
        message = f"Reloaded {self.doc.path}"
        if self._load_issues:
            message += f" ({len(self._load_issues)} problem(s), press c to view)"
        self.notify(message)

    def action_quit(self) -> None:
        self.doc.save()
        self.exit()
