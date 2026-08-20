"""Helpers for linking a task to an external Markdown note (e.g. a file in
an Obsidian vault), opened in VS Code via the `V` key.

Kept UI-agnostic and curses-free, like model.py/view.py/report.py, so it's
testable on its own and reusable by any future front-end.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

# characters illegal in a Windows filename, plus control characters
_INVALID_CHARS = '<>:"/\\|?*'


def safe_filename(name: str) -> str:
    """Turn a task name into a filesystem-safe base filename (no
    extension): illegal/control characters become `-`, runs of whitespace
    collapse to one space, and leading/trailing dots and spaces (which
    Windows silently strips, and which are confusing either way) are
    trimmed. Falls back to "untitled" if that leaves nothing."""
    cleaned = "".join("-" if c in _INVALID_CHARS or ord(c) < 32 else c
                       for c in name)
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned or "untitled"


def note_path(notes_dir: Path, task_name: str) -> Path:
    """The .md file a task's note lives at, inside ``notes_dir``."""
    return notes_dir / f"{safe_filename(task_name)}.md"


def note_header(filename: str, project_name: str, task_name: str,
                now: datetime | None = None) -> str:
    """The header block written to a brand-new task note: a title line,
    the date, and the originating project/task -- so the note is
    traceable back to orgtime even once it grows well beyond this."""
    now = now or datetime.now()
    return (
        f"# {filename}\n"
        f"{now.strftime('%Y-%m-%d')}\n"
        f"Project: {project_name}\n"
        f"Task: {task_name}\n"
        "\n"
    )
