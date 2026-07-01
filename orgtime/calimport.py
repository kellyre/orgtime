"""Import clock entries from an Outlook calendar CSV export.

UI-agnostic core: parse the CSV into events and plan which ones are import
candidates for a date range, applying the blackout rule.  The interactive
decisions (keep/all/skip/ignore-all, project/task choice, overlap handling)
live in the curses front-end.

Outlook's ``Show time as`` is a numeric ``BusyStatus``:
``0 Free, 1 Tentative, 2 Busy, 3 Out of Office, 4 Working Elsewhere``.
Which value counts as the blackout ("Out of Office") code differs between
tenants (legacy uses 4, the newer standard uses 3), so it is configurable.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime

STATUS_NAMES = {
    0: "Free",
    1: "Tentative",
    2: "Busy",
    3: "Out of Office",
    4: "Working Elsewhere",
}

# accepted header spellings (normalised: lowercased, stripped, spaces collapsed)
_REQUIRED = {
    "subject": "subject",
    "start date": "start_date",
    "start time": "start_time",
    "end date": "end_date",
    "end time": "end_time",
}
_OPTIONAL = {
    "all day event": "all_day",
    "show time as": "status",
}

_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d")
_TIME_FORMATS = ("%I:%M:%S %p", "%I:%M %p", "%H:%M:%S", "%H:%M")


@dataclass
class CalEvent:
    subject: str
    start: datetime
    end: datetime
    all_day: bool
    status: int | None
    lineno: int

    def status_label(self, blackout_code: int | None = None) -> str:
        if self.status is None:
            return "?"
        name = STATUS_NAMES.get(self.status, "?")
        tag = " (blackout)" if self.status == blackout_code else ""
        return f"{self.status}:{name}{tag}"


def _norm(header: str) -> str:
    return " ".join(header.strip().lower().split())


def _parse_dt(date_str: str, time_str: str) -> datetime | None:
    date_str, time_str = date_str.strip(), time_str.strip()
    day = None
    for fmt in _DATE_FORMATS:
        try:
            day = datetime.strptime(date_str, fmt).date()
            break
        except ValueError:
            continue
    if day is None:
        return None
    if not time_str:
        return datetime(day.year, day.month, day.day, 0, 0)
    for fmt in _TIME_FORMATS:
        try:
            t = datetime.strptime(time_str, fmt).time()
            return datetime(day.year, day.month, day.day, t.hour, t.minute)
        except ValueError:
            continue
    return None


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("true", "yes", "1")


def parse_csv(text: str) -> tuple[list[CalEvent], list[str]]:
    """Parse Outlook CSV text into events.  Returns (events, problems)."""
    issues: list[str] = []
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return [], ["empty CSV"]

    index: dict[str, int] = {}
    for i, col in enumerate(header):
        key = _norm(col)
        if key in _REQUIRED:
            index[_REQUIRED[key]] = i
        elif key in _OPTIONAL:
            index[_OPTIONAL[key]] = i

    missing = [name for name in _REQUIRED.values() if name not in index]
    if missing:
        return [], [f"missing required column(s): {', '.join(sorted(missing))}"]

    events: list[CalEvent] = []
    for lineno, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue

        def cell(name: str) -> str:
            i = index.get(name)
            return row[i] if i is not None and i < len(row) else ""

        subject = cell("subject").strip()
        start = _parse_dt(cell("start_date"), cell("start_time"))
        end = _parse_dt(cell("end_date"), cell("end_time"))
        if start is None or end is None:
            issues.append(f"line {lineno}: unparseable start/end date-time, skipped")
            continue
        if end < start:
            issues.append(f"line {lineno}: end before start ({subject!r}), skipped")
            continue

        status = None
        raw_status = cell("status").strip()
        if raw_status:
            try:
                status = int(raw_status)
            except ValueError:
                issues.append(f"line {lineno}: non-numeric 'Show time as' {raw_status!r}")

        events.append(CalEvent(
            subject=subject, start=start, end=end,
            all_day=_parse_bool(cell("all_day")), status=status, lineno=lineno))
    return events, issues


def _overlaps_any(event: CalEvent, windows: list[tuple[datetime, datetime]]) -> bool:
    return any(event.start < w_end and w_start < event.end
              for w_start, w_end in windows)


@dataclass
class ImportPlan:
    candidates: list[CalEvent]                 # to offer, in chronological order
    ignored_blackout: list[CalEvent]           # masked by a blackout window
    windows: list[tuple[datetime, datetime]]   # blackout windows


def plan_import(events: list[CalEvent], start_date: date, end_date: date,
                blackout_code: int) -> ImportPlan:
    """Decide which in-range events are import candidates.

    - Blackout windows come from *every* event whose status is the blackout
      code (any date, so a multi-day OOF still masks correctly).
    - All-day events are never imported (used only as blackout windows).
    - A timed blackout event is itself a candidate (and forms a window).
    - Any other timed event overlapping a blackout window is auto-ignored.
    - Remaining timed events in range are candidates, sorted by start.
    """
    windows = [(e.start, e.end) for e in events if e.status == blackout_code]

    candidates: list[CalEvent] = []
    ignored: list[CalEvent] = []
    for event in events:
        if not (start_date <= event.start.date() <= end_date):
            continue
        if event.all_day:
            continue  # never imported as a clock
        if event.status == blackout_code:
            candidates.append(event)  # blackout events are imported
        elif _overlaps_any(event, windows):
            ignored.append(event)
        else:
            candidates.append(event)

    candidates.sort(key=lambda e: (e.start, e.end, e.subject))
    return ImportPlan(candidates, ignored, windows)
