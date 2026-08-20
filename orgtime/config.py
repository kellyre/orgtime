"""Small hand-editable settings file: orgtime.cfg next to the .org file.

Plain ``key = value`` lines, in keeping with the rest of the app. Holds the
timeline view's default workday window, plus the notes folder used by `V`
(open a task's Markdown note in VS Code).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_WORKDAY_START = 7
DEFAULT_WORKDAY_END = 18

CONFIG_FILENAME = "orgtime.cfg"


@dataclass
class Config:
    workday_start: int = DEFAULT_WORKDAY_START
    workday_end: int = DEFAULT_WORKDAY_END
    notes_dir: str = ""  # folder of .md files (e.g. an Obsidian vault); "" = unset


def config_path(org_path: Path) -> Path:
    return org_path.parent / CONFIG_FILENAME


def _parse_hour(value: str, fallback: int) -> int:
    try:
        hour = int(value.strip())
    except ValueError:
        return fallback
    return hour if 0 <= hour <= 24 else fallback


def load_config(org_path: Path) -> Config:
    cfg = Config()
    path = config_path(org_path)
    if not path.exists():
        return cfg
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key == "workday_start":
            cfg.workday_start = _parse_hour(value, cfg.workday_start)
        elif key == "workday_end":
            cfg.workday_end = _parse_hour(value, cfg.workday_end)
        elif key == "notes_dir":
            cfg.notes_dir = value.strip()
    return cfg


def save_config(org_path: Path, cfg: Config) -> None:
    config_path(org_path).write_text(
        "# orgtime settings — plain text, hand-editable\n"
        f"workday_start = {cfg.workday_start}\n"
        f"workday_end = {cfg.workday_end}\n"
        f"notes_dir = {cfg.notes_dir}\n",
        encoding="utf-8",
    )
