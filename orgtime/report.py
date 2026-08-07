"""Plain-text time reports for a Document.

UI-agnostic (no curses/textual): both front-ends call :func:`build_report`
to produce the report text and :func:`default_filename` to name the output
file.  A clock entry is attributed, in full, to the date of its *start*
timestamp; entries are included when that start date falls within the
optional ``[start, end]`` range (inclusive; either bound may be None for
open-ended).  Closed entries use their recorded duration; a running entry
is counted up to ``now`` and also listed as a note.

Soft-deleted projects/tasks/clocks are included too (``include_deleted``,
on by default): declutter the live view without losing credit for the
time. Reconstructed from their tombstoned text (see
``model.find_deleted_items``), they're labeled "(deleted)" in the by-project
and by-project-and-task sections so the report stays honest about current
state while still counting the time toward every total.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from .model import DAY_NAMES, Document, find_deleted_items, format_duration, human_duration

LABEL_WIDTH = 44


def _in_range(d: date, start: date | None, end: date | None) -> bool:
    if start is not None and d < start:
        return False
    if end is not None and d > end:
        return False
    return True


def collect(doc: Document, start: date | None = None, end: date | None = None,
            now: datetime | None = None, include_deleted: bool = True):
    """Return (entries, now) where entries are (project, task, clock, duration).

    With ``include_deleted`` (the default), soft-deleted clocks/tasks/
    projects are reconstructed and included too: a deleted clock is
    attributed to its still-live task/project; a deleted task to its
    still-live project; a deleted project (and everything under it) has
    no live project to attach to, so it appears under its own reconstructed
    name. ``build_report`` labels anything not found in the live tree as
    "(deleted)".
    """
    now = now or datetime.now()
    entries = []
    for project in doc.projects:
        for task in project.tasks:
            for clock in task.clocks:
                if _in_range(clock.start.date(), start, end):
                    entries.append((project, task, clock, clock.duration(now)))
    if include_deleted:
        for item in find_deleted_items(doc, now):
            if item.kind == "clock":
                clock = item.obj
                task = item.owner
                project = doc.project_of(task)
                if project is not None and _in_range(clock.start.date(), start, end):
                    entries.append((project, task, clock, clock.duration(now)))
            elif item.kind == "task":
                project = item.owner
                for clock in item.obj.clocks:
                    if _in_range(clock.start.date(), start, end):
                        entries.append((project, item.obj, clock, clock.duration(now)))
            elif item.kind == "project":
                project = item.obj
                for task in project.tasks:
                    for clock in task.clocks:
                        if _in_range(clock.start.date(), start, end):
                            entries.append((project, task, clock, clock.duration(now)))
    return entries, now


def effective_range(doc: Document, start: date | None, end: date | None,
                    now: datetime | None = None):
    """Resolve open-ended bounds to the actual min/max entry dates in range."""
    entries, now = collect(doc, start, end, now)
    dates = [c.start.date() for _, _, c, _ in entries]
    eff_start = start or (min(dates) if dates else now.date())
    eff_end = end or (max(dates) if dates else now.date())
    return eff_start, eff_end


def default_filename(doc: Document, start: date | None, end: date | None,
                     now: datetime | None = None) -> str:
    eff_start, eff_end = effective_range(doc, start, end, now)
    return f"orgtime-report-{eff_start:%Y%m%d}_{eff_end:%Y%m%d}.txt"


def _leader(label: str, total: timedelta, indent: int = 0) -> str:
    pad = "  " * indent
    text = pad + label
    text = text[:LABEL_WIDTH]
    dots = max(2, LABEL_WIDTH - len(text) + 1)
    return f"{text} {'.' * dots} {format_duration(total):>7}"


def build_report(doc: Document, start: date | None = None, end: date | None = None,
                 now: datetime | None = None) -> str:
    entries, now = collect(doc, start, end, now)
    eff_start, eff_end = effective_range(doc, start, end, now)

    total = sum((d for _, _, _, d in entries), timedelta())
    by_day: dict = defaultdict(timedelta)
    running = []
    for project, task, clock, dur in entries:
        by_day[clock.start.date()] += dur
        if clock.running:
            running.append((project, task, clock))

    def is_live_project(p) -> bool:
        return any(x is p for x in doc.projects)

    def is_live_task(p, t) -> bool:
        return is_live_project(p) and any(x is t for x in p.tasks)

    def label(name: str, deleted: bool) -> str:
        return f"(deleted) {name}" if deleted else name

    # group by project/task identity, in first-seen order -- entries is the
    # single source of truth (live + deleted alike), so nothing further
    # needs to walk doc.projects directly. Model objects are unhashable, so
    # key on id() rather than the object itself.
    proj_order: list[int] = []
    proj_data: dict[int, list] = {}     # id(project) -> [project, total]
    task_order: dict[int, list] = {}    # id(project) -> [id(task), ...]
    task_data: dict[int, list] = {}     # id(task) -> [task, total]
    for project, task, clock, dur in entries:
        pid = id(project)
        if pid not in proj_data:
            proj_data[pid] = [project, timedelta()]
            proj_order.append(pid)
            task_order[pid] = []
        proj_data[pid][1] += dur

        tid = id(task)
        if tid not in task_data:
            task_data[tid] = [task, timedelta()]
            task_order[pid].append(tid)
        task_data[tid][1] += dur

    out: list[str] = []
    out.append("orgtime time report")
    out.append("=" * 40)
    rng = f"{eff_start:%Y-%m-%d} to {eff_end:%Y-%m-%d}"
    if start is None and end is None:
        rng += "  (all entries)"
    out.append(f"Date range : {rng}")
    out.append(f"Generated  : {now:%Y-%m-%d %H:%M}")
    out.append(f"Entries    : {len(entries)}")
    grand = format_duration(total)
    if total >= timedelta(hours=24):
        grand += f"  ({human_duration(total)})"
    out.append(f"Total time : {grand}")
    out.append("")

    out.append("By project")
    out.append("-" * 40)
    if proj_order:
        for pid in proj_order:
            project, ptotal = proj_data[pid]
            out.append(_leader(label(project.name, not is_live_project(project)),
                               ptotal))
    else:
        out.append("  (no entries in range)")
    out.append("")

    out.append("By project and task")
    out.append("-" * 40)
    if proj_order:
        for pid in proj_order:
            project, ptotal = proj_data[pid]
            out.append(_leader(label(project.name, not is_live_project(project)),
                               ptotal))
            for tid in task_order[pid]:
                task, ttotal = task_data[tid]
                deleted = not is_live_task(project, task)
                out.append(_leader(label(f"{task.status} {task.name}", deleted),
                                   ttotal, indent=1))
    else:
        out.append("  (no entries in range)")
    out.append("")

    out.append("By day")
    out.append("-" * 40)
    for day in sorted(by_day):
        day_label = f"{day:%Y-%m-%d} {DAY_NAMES[day.weekday()]}"
        out.append(_leader(day_label, by_day[day]))
    if not by_day:
        out.append("  (no entries in range)")
    out.append("")

    if running:
        out.append("Note: running clock(s) counted up to generation time:")
        for project, task, clock in running:
            out.append(f"  - {task.name} ({project.name}), "
                       f"since {clock.start:%Y-%m-%d %H:%M}")
        out.append("")

    out.append("Durations are hours:minutes; each entry is counted on its "
               "start date. Deleted projects/tasks are labeled \"(deleted)\" "
               "but their time still counts toward every total.")
    return "\n".join(out) + "\n"
