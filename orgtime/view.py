"""UI-agnostic view helpers shared by the curses front-end.

This module deliberately imports neither ``curses`` nor ``textual`` so it
can be unit-tested anywhere.  It turns a :class:`~orgtime.model.Document`
into a flat list of display rows (respecting collapse state) and builds the
plain-text label for each row.  The curses layer adds colour on top.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .model import (
    ClockEntry,
    Document,
    Project,
    Task,
    clock_warnings,
    format_duration,
    human_duration,
)

PROJECT, TASK, CLOCK, COMMENT = "project", "task", "clock", "comment"


class CommentRef:
    """Marker tying a displayed comment line back to its owning item.

    Every line of one comment block shares a CommentRef for the same owner,
    so edit/delete on any line acts on the whole adjoining block.
    """

    def __init__(self, owner: Project | Task | ClockEntry) -> None:
        self.owner = owner


@dataclass
class Row:
    obj: object          # Project | Task | ClockEntry | CommentRef
    kind: str
    depth: int
    text: str            # plain-text label (indentation already applied)
    warn: bool = False    # implausible clock entry?
    running: bool = False  # a running clock?


def _indent(depth: int) -> str:
    return "  " * depth


SORT_MODES = ["file", "priority", "created", "modified"]


def sorted_projects(doc: Document, mode: str) -> list[Project]:
    """Projects in display order for the given sort mode (view-only)."""
    projects = list(doc.projects)
    if mode == "priority":
        return sorted(projects, key=lambda p: (p.priority, p.name.lower()))
    if mode == "created":  # oldest first
        return sorted(projects, key=lambda p: (p.created or datetime.min,
                                               p.name.lower()))
    if mode == "modified":  # most recently changed first
        return sorted(projects, key=lambda p: (p.modified or datetime.min,
                                               p.name.lower()), reverse=True)
    return projects  # "file"


def project_text(project: Project, now: datetime, sort_mode: str = "file") -> str:
    marker = "+" if project.collapsed else "-"
    total = project.total_time(now)
    time_part = f"  {human_duration(total)}" if total else ""
    n = len(project.tasks)
    stamp = ""
    if sort_mode in ("created", "modified"):
        ts = project.created if sort_mode == "created" else project.modified
        if ts is not None:
            stamp = f"  ({sort_mode[0]}:{ts:%Y-%m-%d %H:%M})"
    return (f"{marker} #{project.priority} {project.name}  "
            f"({n} task{'s' if n != 1 else ''}){time_part}{stamp}")


def task_text(task: Task, now: datetime) -> str:
    marker = "+" if (task.collapsed and (task.comments or task.clocks)) else "-"
    total = task.total_time(now)
    time_part = f"  {human_duration(total)}" if total else ""
    run = "  *RUNNING*" if task.running_clock() else ""
    return (f"{_indent(1)}{marker} {task.status} #{task.priority} "
            f"{task.name}{time_part}{run}")


def clock_text(clock: ClockEntry, now: datetime) -> str:
    from .model import format_ts
    warn = "  !" if clock_warnings(clock, now) else ""
    if clock.running:
        return (f"{_indent(2)}CLOCK: {format_ts(clock.start)}--... "
                f"running {human_duration(clock.duration(now))}{warn}")
    return (f"{_indent(2)}CLOCK: {format_ts(clock.start)}--"
            f"{format_ts(clock.end)} => {format_duration(clock.duration())}{warn}")


def comment_text(text: str, depth: int) -> str:
    return f"{_indent(depth)}# {text}" if text else f"{_indent(depth)}#"


@dataclass
class SearchTarget:
    """A searchable location in the document, in display order.

    ``owner`` is the object to reveal/select: the project or task itself for
    those kinds, or the comment's owning project/task/clock for COMMENT.
    ``project``/``task`` are the ancestors that must be expanded to show it.
    """

    text: str
    project: Project
    task: Task | None
    kind: str
    owner: object


def search_targets(doc: Document) -> list[SearchTarget]:
    """All searchable items (project names, task names, comment lines) in
    document order, regardless of collapse state."""
    targets: list[SearchTarget] = []
    for project in doc.projects:
        targets.append(SearchTarget(project.name, project, None, PROJECT, project))
        for text in project.comments:
            targets.append(SearchTarget(text, project, None, COMMENT, project))
        for task in project.tasks:
            targets.append(SearchTarget(task.name, project, task, TASK, task))
            for text in task.comments:
                targets.append(SearchTarget(text, project, task, COMMENT, task))
            for clock in task.clocks:
                for text in clock.comments:
                    targets.append(
                        SearchTarget(text, project, task, COMMENT, clock))
    return targets


def next_match_index(targets: list[SearchTarget], term: str,
                     start: int = -1) -> int | None:
    """Index of the next target after ``start`` whose text contains ``term``
    (case-insensitive), wrapping around.  None if nothing matches."""
    term = term.lower()
    n = len(targets)
    for offset in range(1, n + 1):
        i = (start + offset) % n
        if term in targets[i].text.lower():
            return i
    return None


def flatten(doc: Document, now: datetime | None = None,
            sort_mode: str = "file") -> list[Row]:
    """Walk the document into visible rows, honouring collapse state.

    Project.collapsed hides everything beneath it; Task.collapsed hides the
    task's comments, clock entries, and clock-attached comments.
    ``sort_mode`` reorders projects for display only (see ``sorted_projects``).
    """
    now = now or datetime.now()
    rows: list[Row] = []

    def add_comments(owner, depth: int) -> None:
        ref = CommentRef(owner)
        for text in owner.comments:
            rows.append(Row(ref, COMMENT, depth, comment_text(text, depth)))

    for project in sorted_projects(doc, sort_mode):
        rows.append(Row(project, PROJECT, 0,
                        project_text(project, now, sort_mode)))
        if project.collapsed:
            continue
        add_comments(project, 1)
        for task in project.tasks:
            warn = any(clock_warnings(c, now) for c in task.clocks)
            rows.append(Row(task, TASK, 1, task_text(task, now),
                            running=task.running_clock() is not None, warn=warn))
            if task.collapsed:
                continue
            add_comments(task, 2)
            for clock in task.clocks:
                rows.append(Row(clock, CLOCK, 2, clock_text(clock, now),
                                warn=bool(clock_warnings(clock, now)),
                                running=clock.running))
                add_comments(clock, 3)
    return rows


HELP_LINES = [
    "orgtime (curses)  —  keys",
    "",
    "  Up/Down, j/k     move cursor      Home/End, g/G  top/bottom",
    "  Enter / Space    collapse/expand  Tab            collapse/expand",
    "  J                jump to running clock   C       collapse all projects",
    "  z                sort projects (file/priority/created/modified)",
    "  A                import Outlook calendar CSV (appointments)",
    "  /                search (repeat with /)  m       move task to project",
    "  N                new project      n              new task",
    "  e                edit item        d              delete (soft)",
    "  c                add/edit comment X              expunge ## lines",
    "  i / o            clock in / out   I / O          clock in/out at time",
    "  s / S            status fwd/back  D              mark task/project DONE",
    "  1-5              set priority",
    "  u / U            undo / redo      v              verify (consistency)",
    "  r                write report     L              reload file",
    "  q                quit (saves)     ?              this help",
    "",
    "  Comments are edited in a multi-line box: Ctrl+O saves, Esc cancels.",
    "  Deletes are soft: lines get ## prepended and stay in the file.",
    "  Press any key to close this help.",
]
