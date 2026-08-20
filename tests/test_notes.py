"""Tests for orgtime/notes.py -- task-note filename/header helpers."""

from datetime import datetime
from pathlib import Path

from orgtime.notes import note_header, note_path, safe_filename


def test_safe_filename_passes_through_normal_names():
    assert safe_filename("Design mockups") == "Design mockups"


def test_safe_filename_replaces_illegal_windows_characters():
    assert safe_filename('Fix: "the" bug <now>?') == "Fix- -the- bug -now--"


def test_safe_filename_collapses_whitespace_and_trims_dots_spaces():
    assert safe_filename("  spaced   out  ") == "spaced out"
    assert safe_filename("trailing dot.") == "trailing dot"
    assert safe_filename("...") == "untitled"


def test_safe_filename_empty_falls_back_to_untitled():
    assert safe_filename("") == "untitled"
    assert safe_filename("   ") == "untitled"


def test_note_path_joins_dir_and_safe_filename():
    p = note_path(Path("/vault"), "Fix: the bug")
    assert p == Path("/vault") / "Fix- the bug.md"


def test_note_header_format():
    text = note_header("Design mockups", "Website", "Design mockups",
                       datetime(2026, 8, 20, 9, 30))
    assert text == (
        "# Design mockups\n"
        "2026-08-20\n"
        "Project: Website\n"
        "Task: Design mockups\n"
        "\n"
    )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
