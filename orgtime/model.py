"""Data model, org-like file format, and consistency checking for orgtime.

File format (a simplified org-mode):

    * [#2] Project name
    # a comment line attached to the project
    ** TODO [#1] Task name
    # a comment attached to the task
       CLOCK: [2026-06-10 Wed 09:00]--[2026-06-10 Wed 10:30] => 1:30
    # a comment attached to that clock entry
    ## ** TODO a soft-deleted task line
    ### a soft-deleted comment line

A CLOCK line with no end timestamp is a running clock.

Comment lines start with a single ``#`` and attach to the nearest item
above them (project, task, or clock entry).  Lines starting with two or
more ``#`` are soft-deleted ("tombstones"): invisible in the UI but kept
verbatim in the file until expunged.  Deleting anything prepends ``##``
to its lines rather than removing them, so a deleted comment ends up
with ``###``.

Legacy ``:DESCRIPTION:`` lines (descriptions are no longer a feature) are
migrated to comments on the nearest project/task when a file is loaded.

The file is meant to be hand-editable; ``parse()`` collects problems
instead of crashing, and ``check_consistency()`` flags semantic errors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

STATUSES = ["TODO", "IN-PROGRESS", "HOLD", "CANCELLED", "DONE"]
CLOSED_STATUSES = {"CANCELLED", "DONE"}
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

TS_FORMAT = "%Y-%m-%d %H:%M"  # what users type in edit dialogs

_TS_RE = r"\[(\d{4}-\d{2}-\d{2})(?: ([A-Za-z]{2,3}))? (\d{1,2}:\d{2})\]"
_CLOCK_RE = re.compile(
    rf"^\s*CLOCK:\s*{_TS_RE}(?:--{_TS_RE}(?:\s*=>\s*(\d+):(\d{{2}}))?)?\s*$"
)
_PROJECT_RE = re.compile(r"^\*\s+(?:\[#(\d+)\]\s+)?(.+?)\s*$")
_TASK_RE = re.compile(r"^\*\*\s+(?:(\S+)\s+)?(?:\[#(\d+)\]\s+)?(.+?)\s*$")
_DESC_RE = re.compile(r"^\s*:DESCRIPTION:\s?(.*?)\s*$")
_CREATED_RE = re.compile(r":CREATED:\s*" + _TS_RE)
_MODIFIED_RE = re.compile(r":MODIFIED:\s*" + _TS_RE)


def parse_user_ts(text: str, base: datetime | None = None) -> datetime | None:
    """Parse a user-typed timestamp.

    Accepts the full 'YYYY-MM-DD HH:MM' or a bare 'HH:MM' (the date is then
    taken from ``base``, defaulting to today).  Returns None if unparseable.
    """
    text = text.strip()
    try:
        return datetime.strptime(text, TS_FORMAT)
    except ValueError:
        pass
    try:
        clock_time = datetime.strptime(text, "%H:%M").time()
    except ValueError:
        return None
    return datetime.combine((base or datetime.now()).date(), clock_time)


def parse_user_date(text: str):
    """Parse a user-typed date ('YYYY-MM-DD' or 'YYYY/MM/DD').

    Returns a ``datetime.date`` or None if it cannot be parsed.
    """
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def format_ts(dt: datetime) -> str:
    return f"[{dt.strftime('%Y-%m-%d')} {DAY_NAMES[dt.weekday()]} {dt.strftime('%H:%M')}]"


def format_duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60}:{minutes % 60:02d}"


# closed entries at least this long are flagged as suspicious
SUSPICIOUS_DURATION = timedelta(hours=24)


def human_duration(delta: timedelta) -> str:
    """Duration for display: '7:05', or '2d 22:43' once it exceeds a day."""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 24 * 60:
        return f"{minutes // 60}:{minutes % 60:02d}"
    days, rest = divmod(minutes, 24 * 60)
    return f"{days}d {rest // 60}:{rest % 60:02d}"


def clock_warnings(clock: ClockEntry, now: datetime | None = None) -> list[str]:
    """Plausibility problems with one clock entry (empty list = looks fine)."""
    now = now or datetime.now()
    problems = []
    if clock.start > now:
        problems.append(f"starts in the future ({format_ts(clock.start)})")
    if clock.end is not None:
        if clock.end > now:
            problems.append(f"ends in the future ({format_ts(clock.end)})")
        if clock.end >= clock.start and clock.duration() >= SUSPICIOUS_DURATION:
            problems.append(
                f"suspiciously long: {human_duration(clock.duration())} "
                f"(more than 24 hours)"
            )
    elif now - clock.start >= SUSPICIOUS_DURATION:
        problems.append(
            f"running for {human_duration(now - clock.start)} — forgotten clock-out?"
        )
    return problems


def comment_lines(comments: list[str]) -> list[str]:
    return [f"# {c}".rstrip() for c in comments]


def tombstoned(lines: list[str]) -> list[str]:
    """Prefix lines with ## for soft deletion (comments get ### total)."""
    return [("##" + line) if line.startswith("#") else ("## " + line)
            for line in lines]


@dataclass
class ClockEntry:
    start: datetime
    end: datetime | None = None  # None = running
    comments: list[str] = field(default_factory=list)
    tombstones: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.end is None

    def duration(self, now: datetime | None = None) -> timedelta:
        end = self.end or now or datetime.now()
        return end - self.start

    def serialize(self) -> str:
        line = f"CLOCK: {format_ts(self.start)}"
        if self.end is not None:
            line += f"--{format_ts(self.end)} => {format_duration(self.duration())}"
        return line

    def lines(self) -> list[str]:
        return [f"   {self.serialize()}"] + comment_lines(self.comments) + self.tombstones


def _meta_line(indent: str, created: datetime | None,
               modified: datetime | None) -> list[str]:
    parts = []
    if created is not None:
        parts.append(f":CREATED: {format_ts(created)}")
    if modified is not None:
        parts.append(f":MODIFIED: {format_ts(modified)}")
    return [indent + " ".join(parts)] if parts else []


@dataclass
class Task:
    name: str
    status: str = "TODO"
    priority: int = 3
    created: datetime | None = None
    modified: datetime | None = None
    comments: list[str] = field(default_factory=list)
    tombstones: list[str] = field(default_factory=list)
    clocks: list[ClockEntry] = field(default_factory=list)
    collapsed: bool = True  # UI state, not saved

    def total_time(self, now: datetime | None = None) -> timedelta:
        return sum((c.duration(now) for c in self.clocks), timedelta())

    def running_clock(self) -> ClockEntry | None:
        return next((c for c in self.clocks if c.running), None)

    def lines(self) -> list[str]:
        out = [f"** {self.status} [#{self.priority}] {self.name}"]
        out += _meta_line("   ", self.created, self.modified)
        out += comment_lines(self.comments)
        out += self.tombstones
        for clock in self.clocks:
            out += clock.lines()
        return out


@dataclass
class Project:
    name: str
    priority: int = 3
    created: datetime | None = None
    modified: datetime | None = None
    comments: list[str] = field(default_factory=list)
    tombstones: list[str] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    collapsed: bool = False  # UI state, not saved

    def total_time(self, now: datetime | None = None) -> timedelta:
        return sum((t.total_time(now) for t in self.tasks), timedelta())

    def lines(self) -> list[str]:
        out = [f"* [#{self.priority}] {self.name}"]
        out += _meta_line("  ", self.created, self.modified)
        out += comment_lines(self.comments)
        out += self.tombstones
        for task in self.tasks:
            out += task.lines()
        return out


@dataclass
class Document:
    projects: list[Project] = field(default_factory=list)
    tombstones: list[str] = field(default_factory=list)  # serialized at end of file
    path: Path | None = None

    # -- queries ----------------------------------------------------------

    def running(self) -> tuple[Project, Task, ClockEntry] | None:
        for project in self.projects:
            for task in project.tasks:
                clock = task.running_clock()
                if clock is not None:
                    return project, task, clock
        return None

    def project_of(self, task: Task) -> Project | None:
        return next((p for p in self.projects if task in p.tasks), None)

    def task_of(self, clock: ClockEntry) -> Task | None:
        for project in self.projects:
            for task in project.tasks:
                if clock in task.clocks:
                    return task
        return None

    def project_of_clock(self, clock: ClockEntry) -> Project | None:
        task = self.task_of(clock)
        return self.project_of(task) if task else None

    def find_clock(self, start: datetime, end: datetime) -> ClockEntry | None:
        """An existing closed clock with exactly these start/end times, if any
        (used to skip re-importing a duplicate calendar entry)."""
        for project in self.projects:
            for task in project.tasks:
                for clock in task.clocks:
                    if clock.start == start and clock.end == end:
                        return clock
        return None

    # -- modified-time bookkeeping ----------------------------------------

    def touch(self, obj, now: datetime | None = None) -> None:
        """Bump modified time on ``obj`` and its owning task/project.

        ``obj`` may be a Project, Task, or ClockEntry. Touching a task or
        clock also touches the owning project.
        """
        now = (now or datetime.now()).replace(second=0, microsecond=0)
        if isinstance(obj, ClockEntry):
            obj = self.task_of(obj)
            if obj is None:
                return
        if isinstance(obj, Task):
            obj.modified = now
            project = self.project_of(obj)
            if project is not None:
                project.modified = now
        elif isinstance(obj, Project):
            obj.modified = now

    # -- clocking ---------------------------------------------------------

    def clock_in(self, task: Task, now: datetime | None = None) -> None:
        now = (now or datetime.now()).replace(second=0, microsecond=0)
        self.clock_out(now)
        task.clocks.append(ClockEntry(start=now))
        if task.status not in CLOSED_STATUSES:
            task.status = "IN-PROGRESS"

    def clock_out(self, now: datetime | None = None) -> Task | None:
        now = (now or datetime.now()).replace(second=0, microsecond=0)
        active = self.running()
        if active is None:
            return None
        _, task, clock = active
        clock.end = max(now, clock.start)
        return task

    # -- soft deletion ------------------------------------------------------

    def _tombstone_lists(self):
        yield self.tombstones
        for project in self.projects:
            yield project.tombstones
            for task in project.tasks:
                yield task.tombstones
                for clock in task.clocks:
                    yield clock.tombstones

    def tombstone_count(self) -> int:
        return sum(len(lst) for lst in self._tombstone_lists())

    def expunge(self) -> int:
        """Permanently remove all soft-deleted (##) lines.  Returns count."""
        count = self.tombstone_count()
        for lst in self._tombstone_lists():
            lst.clear()
        return count

    # -- persistence ------------------------------------------------------

    def serialize(self) -> str:
        blocks = ["\n".join(project.lines()) for project in self.projects]
        if self.tombstones:
            blocks.append("\n".join(self.tombstones))
        return "\n\n".join(blocks) + ("\n" if blocks else "")

    def save(self, path: Path | None = None) -> None:
        path = path or self.path
        if path is None:
            raise ValueError("no path to save to")
        path.write_text(self.serialize(), encoding="utf-8")
        self.path = path


# -- parsing ---------------------------------------------------------------


def _parse_ts(date_s: str, time_s: str, lineno: int, issues: list[str]) -> datetime | None:
    try:
        return datetime.strptime(f"{date_s} {time_s}", TS_FORMAT)
    except ValueError:
        issues.append(f"line {lineno}: invalid timestamp [{date_s} {time_s}]")
        return None


def _parse_priority(raw: str | None, lineno: int, issues: list[str]) -> int:
    if raw is None:
        return 3
    value = int(raw)
    if not 1 <= value <= 5:
        issues.append(f"line {lineno}: priority [#{value}] out of range 1-5, using 3")
        return 3
    return value


def parse(text: str) -> tuple[Document, list[str]]:
    """Parse file text.  Returns (document, list of format problems found)."""
    doc = Document()
    issues: list[str] = []
    current: Project | Task | None = None       # target for :DESCRIPTION:
    last_object: Project | Task | ClockEntry | None = None  # anchor for comments

    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue

        if line.startswith("** "):
            match = _TASK_RE.match(line)
            if match is None:
                issues.append(f"line {lineno}: unparseable task line: {stripped}")
                continue
            if not doc.projects:
                issues.append(f"line {lineno}: task appears before any project, skipped")
                continue
            status_word, prio_raw, name = match.groups()
            if status_word in STATUSES:
                status = status_word
            else:
                status = "TODO"
                prio_match = re.fullmatch(r"\[#(\d+)\]", status_word or "")
                if prio_match and prio_raw is None:
                    prio_raw = prio_match.group(1)  # was the priority, not a status
                else:
                    # status word was actually part of the name
                    name = f"{status_word} {name}" if status_word else name
                issues.append(
                    f"line {lineno}: missing or unknown status "
                    f"{status_word!r}, defaulting to TODO"
                )
            task = Task(
                name=name,
                status=status,
                priority=_parse_priority(prio_raw, lineno, issues),
            )
            doc.projects[-1].tasks.append(task)
            current = last_object = task

        elif line.startswith("* "):
            match = _PROJECT_RE.match(line)
            if match is None:
                issues.append(f"line {lineno}: unparseable project line: {stripped}")
                continue
            prio_raw, name = match.groups()
            project = Project(name=name, priority=_parse_priority(prio_raw, lineno, issues))
            doc.projects.append(project)
            current = last_object = project

        elif stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            if hashes == 1:
                comment_text = stripped[1:]
                if comment_text.startswith(" "):
                    comment_text = comment_text[1:]
                if last_object is None:
                    issues.append(
                        f"line {lineno}: comment before any project — "
                        f"soft-deleted and kept at end of file"
                    )
                    doc.tombstones.append("##" + stripped)
                else:
                    last_object.comments.append(comment_text)
            else:
                # soft-deleted line: keep verbatim, anchored to the item above
                anchor = last_object if last_object is not None else doc
                anchor.tombstones.append(stripped)

        elif stripped.startswith(":CREATED:") or stripped.startswith(":MODIFIED:"):
            if current is None:
                issues.append(f"line {lineno}: created/modified before any project/task")
                continue
            cm = _CREATED_RE.search(line)
            mm = _MODIFIED_RE.search(line)
            if cm:
                current.created = _parse_ts(cm.group(1), cm.group(3), lineno, issues)
            if mm:
                current.modified = _parse_ts(mm.group(1), mm.group(3), lineno, issues)

        elif _DESC_RE.match(line):
            # legacy :DESCRIPTION: lines are migrated to comments on the
            # nearest project/task (descriptions are no longer a feature)
            if current is None:
                issues.append(f"line {lineno}: :DESCRIPTION: before any project/task")
                continue
            text_part = _DESC_RE.match(line).group(1)
            if text_part:
                current.comments.append(text_part)

        elif stripped.startswith("CLOCK:"):
            match = _CLOCK_RE.match(line)
            if match is None:
                issues.append(f"line {lineno}: malformed CLOCK line: {stripped}")
                continue
            if not isinstance(current, Task):
                issues.append(f"line {lineno}: CLOCK line outside a task, skipped")
                continue
            d1, day1, t1, d2, day2, t2, dur_h, dur_m = match.groups()
            start = _parse_ts(d1, t1, lineno, issues)
            if start is None:
                continue
            end = None
            if d2 is not None:
                end = _parse_ts(d2, t2, lineno, issues)
                if end is None:
                    continue
            entry = ClockEntry(start=start, end=end)
            current.clocks.append(entry)
            last_object = entry
            # stash details the consistency check needs
            entry._source = (lineno, day1, day2, dur_h, dur_m)  # type: ignore[attr-defined]

        else:
            issues.append(f"line {lineno}: unrecognized line: {stripped}")

    return doc, issues


def _recent_clock_time(clocks: list[ClockEntry]) -> datetime | None:
    times: list[datetime] = []
    for clock in clocks:
        times.append(clock.start)
        if clock.end is not None:
            times.append(clock.end)
    return max(times) if times else None


def resolve_times(doc: Document, now: datetime | None = None) -> None:
    """Fill in any missing created/modified times in place.

    A missing time defaults to the most recent clock timestamp of the item
    (for a project, across all its tasks' clocks), or ``now`` if there are
    no clocks.
    """
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    for project in doc.projects:
        # project default = most recent ACTUAL clock across its tasks, else now
        all_clocks = [c for task in project.tasks for c in task.clocks]
        project_default = _recent_clock_time(all_clocks) or now
        if project.created is None:
            project.created = project_default
        if project.modified is None:
            project.modified = project_default
        for task in project.tasks:
            task_default = _recent_clock_time(task.clocks) or now
            if task.created is None:
                task.created = task_default
            if task.modified is None:
                task.modified = task_default


def load(path: Path) -> tuple[Document, list[str]]:
    if path.exists():
        doc, issues = parse(path.read_text(encoding="utf-8"))
    else:
        doc, issues = Document(), []
    doc.path = path
    resolve_times(doc)
    return doc, issues


def make_backup(path: Path | None) -> Path | None:
    """Copy ``path`` to backups/<stem>_<YYYYMMDD-HHMMSS><suffix>.

    Returns the backup path, or None if there was nothing to back up.
    """
    if path is None or not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backups = path.parent / "backups"
    backups.mkdir(exist_ok=True)
    dest = backups / f"{path.stem}_{stamp}{path.suffix}"
    dest.write_bytes(path.read_bytes())
    return dest


# -- overlap resolution -----------------------------------------------------


@dataclass
class ClockChange:
    """A planned change to one existing clock entry so it no longer overlaps
    an edited entry.  ``split_extra`` (start, end), when set, is a second
    interval to add to the same task (the entry was split in two)."""

    project: "Project"
    task: "Task"
    clock: "ClockEntry"
    old_start: datetime
    old_end: datetime
    new_start: datetime
    new_end: datetime
    becomes_zero: bool
    split_extra: tuple[datetime, datetime] | None = None


def overlap_changes(doc: "Document", edited: "ClockEntry",
                    new_start: datetime, new_end: datetime) -> list[ClockChange]:
    """Plan the changes to *other* closed clock entries so none overlaps the
    edited entry's new ``[new_start, new_end]`` interval.

    - an entry that strictly contains the new interval is split in two;
    - an entry overlapping on one side is trimmed to that side;
    - an entry fully inside the new interval collapses to a zero-length slot
      (flagged with ``becomes_zero``).

    The edited entry itself and any running (open) entries are ignored.
    """
    changes: list[ClockChange] = []
    for project in doc.projects:
        for task in project.tasks:
            for clock in task.clocks:
                if clock is edited or clock.end is None:
                    continue
                os_, oe = clock.start, clock.end
                if not (os_ < new_end and new_start < oe):
                    continue  # no positive overlap
                if os_ < new_start and new_end < oe:
                    change = ClockChange(project, task, clock, os_, oe,
                                         os_, new_start, False, (new_end, oe))
                elif os_ < new_start:               # left overlap -> trim end
                    change = ClockChange(project, task, clock, os_, oe,
                                         os_, new_start, False, None)
                elif new_end < oe:                  # right overlap -> trim start
                    change = ClockChange(project, task, clock, os_, oe,
                                         new_end, oe, False, None)
                else:                               # fully inside -> collapse
                    change = ClockChange(project, task, clock, os_, oe,
                                         os_, os_, True, None)
                changes.append(change)
    changes.sort(key=lambda c: c.old_start)
    return changes


def apply_overlap_changes(changes: list[ClockChange]) -> None:
    """Apply a plan produced by :func:`overlap_changes` in place."""
    for change in changes:
        change.clock.start = change.new_start
        change.clock.end = change.new_end
        if change.split_extra is not None:
            start, end = change.split_extra
            extra = ClockEntry(start=start, end=end)
            idx = change.task.clocks.index(change.clock)
            change.task.clocks.insert(idx + 1, extra)


def _short_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def describe_change(change: ClockChange) -> str:
    head = (f"{change.project.name} / {change.task.name}: "
            f"{_short_ts(change.old_start)}--{_short_ts(change.old_end)} -> ")
    if change.split_extra is not None:
        s2, e2 = change.split_extra
        return (head + f"split into {_short_ts(change.new_start)}--"
                f"{_short_ts(change.new_end)} and {_short_ts(s2)}--{_short_ts(e2)}")
    if change.becomes_zero:
        return head + "0:00 — WIPED OUT (possible typo)"
    return head + f"{_short_ts(change.new_start)}--{_short_ts(change.new_end)}"


def _subtract_intervals(base: tuple[datetime, datetime],
                        holes: list[tuple[datetime, datetime]]):
    """Return the positive-length pieces of ``base`` not covered by ``holes``."""
    s, e = base
    clipped = sorted((max(s, a), min(e, b)) for a, b in holes
                     if max(s, a) < min(e, b))
    pieces = []
    cursor = s
    for a, b in clipped:
        if a > cursor:
            pieces.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < e:
        pieces.append((cursor, e))
    return pieces


def reshape_to_avoid(doc: "Document", edited: "ClockEntry",
                     new_start: datetime, new_end: datetime):
    """Plan for the *others-win* case: the pieces the edited entry must shrink
    to so it sits in the gaps between the other (unchanged) entries.

    Returns a list of (start, end) pieces; an empty list means the edited
    interval is fully covered by others and would be wiped out (0:00).
    """
    holes = []
    for project in doc.projects:
        for task in project.tasks:
            for clock in task.clocks:
                if clock is edited or clock.end is None:
                    continue
                if clock.start < new_end and new_start < clock.end:
                    holes.append((clock.start, clock.end))
    return _subtract_intervals((new_start, new_end), holes)


def apply_reshape(doc: "Document", edited: "ClockEntry",
                  pieces: list[tuple[datetime, datetime]],
                  fallback_start: datetime) -> None:
    """Apply an others-win plan to the edited entry in place.

    The edited entry becomes the first piece; any further pieces are added as
    new clock entries on its task. No pieces => the entry is wiped to a
    zero-length slot at ``fallback_start``.
    """
    task = doc.task_of(edited)
    if not pieces:
        edited.start = edited.end = fallback_start
        return
    edited.start, edited.end = pieces[0]
    if task is not None:
        idx = task.clocks.index(edited)
        for offset, (start, end) in enumerate(pieces[1:], start=1):
            task.clocks.insert(idx + offset, ClockEntry(start=start, end=end))


# -- consistency check ------------------------------------------------------


def check_consistency(doc: Document, now: datetime | None = None) -> list[str]:
    """Semantic checks beyond parse-time format errors."""
    now = now or datetime.now()
    problems: list[str] = []
    open_clocks: list[str] = []

    for project in doc.projects:
        if not project.name.strip():
            problems.append("project with empty name")
        for task in project.tasks:
            label = f"{project.name} / {task.name}"
            if not task.name.strip():
                problems.append(f"{project.name}: task with empty name")
            if task.status not in STATUSES:
                problems.append(f"{label}: unknown status {task.status!r}")
            if not 1 <= task.priority <= 5:
                problems.append(f"{label}: priority {task.priority} out of range 1-5")

            spans: list[tuple[datetime, datetime]] = []
            for clock in task.clocks:
                for warning in clock_warnings(clock, now):
                    problems.append(f"{label}: clock {warning}")
                source = getattr(clock, "_source", None)
                if clock.end is not None:
                    if clock.end < clock.start:
                        problems.append(
                            f"{label}: clock ends before it starts "
                            f"({format_ts(clock.start)}--{format_ts(clock.end)})"
                        )
                    else:
                        spans.append((clock.start, clock.end))
                    if source and source[3] is not None:
                        stated = timedelta(hours=int(source[3]), minutes=int(source[4]))
                        if stated != clock.duration():
                            problems.append(
                                f"{label}: stated duration {source[3]}:{source[4]} "
                                f"!= actual {format_duration(clock.duration())} "
                                f"(line {source[0]})"
                            )
                else:
                    open_clocks.append(f"{label} (started {format_ts(clock.start)})")
                    if task.status in CLOSED_STATUSES:
                        problems.append(f"{label}: running clock on {task.status} task")
                if source:
                    for day_name, dt in ((source[1], clock.start), (source[2], clock.end)):
                        if day_name and dt and day_name != DAY_NAMES[dt.weekday()]:
                            problems.append(
                                f"{label}: day name {day_name!r} does not match date "
                                f"{dt.strftime('%Y-%m-%d')} ({DAY_NAMES[dt.weekday()]}) "
                                f"(line {source[0]})"
                            )

            spans.sort()
            for (s1, e1), (s2, _e2) in zip(spans, spans[1:]):
                if s2 < e1:
                    problems.append(
                        f"{label}: overlapping clock entries around {format_ts(s2)}"
                    )

    if len(open_clocks) > 1:
        problems.append("more than one running clock: " + "; ".join(open_clocks))

    return problems
