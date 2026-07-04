"""Tests for the small orgtime.cfg settings file."""

import tempfile
from pathlib import Path

from orgtime.config import (
    DEFAULT_WORKDAY_START,
    DEFAULT_WORKDAY_END,
    Config,
    config_path,
    load_config,
    save_config,
)


def test_load_defaults_when_missing():
    with tempfile.TemporaryDirectory() as tmp:
        org_path = Path(tmp) / "timelog.org"
        cfg = load_config(org_path)
        assert cfg == Config(DEFAULT_WORKDAY_START, DEFAULT_WORKDAY_END)


def test_save_then_load_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        org_path = Path(tmp) / "timelog.org"
        save_config(org_path, Config(workday_start=6, workday_end=20))
        cfg = load_config(org_path)
        assert cfg.workday_start == 6 and cfg.workday_end == 20
        assert config_path(org_path).name == "orgtime.cfg"
        assert config_path(org_path).parent == org_path.parent


def test_load_tolerates_comments_and_bad_values():
    with tempfile.TemporaryDirectory() as tmp:
        org_path = Path(tmp) / "timelog.org"
        config_path(org_path).write_text(
            "# a comment\n"
            "workday_start = 6  # inline note\n"
            "workday_end = nonsense\n"
            "junk line with no equals\n"
            "workday_end=25\n",   # out of range, ignored
            encoding="utf-8",
        )
        cfg = load_config(org_path)
        assert cfg.workday_start == 6
        assert cfg.workday_end == DEFAULT_WORKDAY_END  # both bad values ignored


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
