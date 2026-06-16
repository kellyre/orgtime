"""Plain-text time reports for a Document.

UI-agnostic (no curses/textual): both front-ends call :func:`build_report`
to produce the report text and :func:`default_filename` to name the output
file.  A clock entry is attributed, in full, to the date of its *start*
timestamp; entries are included when that start date falls within the
optional ``[start, end]`` range (inclusive; either bound may be None for
open-ended).  Closed entries use their recorded duration; a running entry
is counted up to ``now`` and also listed as a note.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from .model import DAY_NAMES, Document, format_duration, human_duration

LABEL_WIDTH = 44


def _in_range(d: date, start: date | None, end: date | None) -> bool:
    if start is not None and d < start:
        return False
    if end is not None and d > end:
        return False
    return True


def collect(doc: Document, start: date | None = None, end: date | None = None,
            now: datetime | None = None):
    """Return (entries, now) where entries are (project, task, clock, duration)."""
    now = now or datetime.now()
    entries = []
    for project in doc.projects:
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


def _task_total(task, start, end, now) -> timedelta:
    return sum((c.duration(now) for c in task.clocks
               if _in_range(c.start.date(), start, end)), timedelta())


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

    # per-project / per-task totals computed structurally (model objects are
    # unhashable, so they can't be dict keys)
    proj_totals = [(p, _proj_total(p, start, end, now)) for p in doc.projects]
    proj_totals = [(p, t) for p, t in proj_totals if t]

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
    if proj_totals:
        for project, ptotal in proj_totals:
            out.append(_leader(project.name, ptotal))
    else:
        out.append("  (no entries in range)")
    out.append("")

    out.append("By project and task")
    out.append("-" * 40)
    if proj_totals:
        for project, ptotal in proj_totals:
            out.append(_leader(project.name, ptotal))
            for task in project.tasks:
                ttotal = _task_total(task, start, end, now)
                if ttotal:
                    out.append(_leader(f"{task.status} {task.name}",
                                       ttotal, indent=1))
    else:
        out.append("  (no entries in range)")
    out.append("")

    out.append("By day")
    out.append("-" * 40)
    for day in sorted(by_day):
        label = f"{day:%Y-%m-%d} {DAY_NAMES[day.weekday()]}"
        out.append(_leader(label, by_day[day]))
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
               "start date.")
    return "\n".join(out) + "\n"


def _proj_total(project, start, end, now) -> timedelta:
    return sum((_task_total(t, start, end, now) for t in project.tasks),
               timedelta())
