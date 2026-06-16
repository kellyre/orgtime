"""Headless smoke test of the TUI using Textual's pilot."""

import asyncio
import tempfile
from pathlib import Path

from textual.widgets import Tree

from orgtime.app import (
    CommentDialog,
    CommentRef,
    EditDialog,
    OrgTimeApp,
    ReportDialog,
    ReportInputDialog,
    TimeDialog,
)
from orgtime.model import parse


def select(app, obj) -> None:
    """Point the tree cursor at the node holding ``obj`` (or its CommentRef)."""
    tree = app.query_one(Tree)
    for line in range(tree.last_line + 1):
        node = tree.get_node_at_line(line)
        data = node.data if node else None
        if data is obj or (isinstance(data, CommentRef) and data.owner is obj):
            tree.cursor_line = line
            return
    raise AssertionError(f"object not found in tree: {obj!r}")


async def run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "timelog.org"
        app = OrgTimeApp(path)
        async with app.run_test(size=(100, 40)) as pilot:
            # new project: type name, enter saves immediately
            await pilot.press("N")
            assert isinstance(app.screen, EditDialog)
            await pilot.press(*"Website Redesign", "enter")
            await pilot.pause()
            assert len(app.doc.projects) == 1

            # new task under it
            await pilot.press("n")
            await pilot.press(*"Design mockups", "enter")
            await pilot.pause()
            task = app.doc.projects[0].tasks[0]
            assert task.name == "Design mockups"

            # edit: tab from name into description TextArea, ctrl+s saves
            select(app, task)
            await pilot.press("e")
            await pilot.press("tab", *"Figma work", "ctrl+s")
            await pilot.pause()
            assert task.description == "Figma work"

            # editing no longer select-alls: typing appends instead of wiping
            await pilot.press("e")
            await pilot.press(*"XY")
            name_input = app.screen.query_one("#name")
            assert name_input.value == "Design mockupsXY"
            await pilot.press("ctrl+z", "ctrl+z")  # field-level text undo
            assert name_input.value == "Design mockups"
            await pilot.press("enter")
            await pilot.pause()
            assert task.name == "Design mockups"

            # clock in / out
            await pilot.press("i")
            await pilot.pause()
            assert task.running_clock() is not None
            assert task.status == "IN-PROGRESS"
            await pilot.press("o")
            await pilot.pause()
            assert app.doc.running() is None

            # clock in again, then clock out at an explicit (current) time
            await pilot.press("i")
            await pilot.pause()
            await pilot.press("O")
            assert isinstance(app.screen, TimeDialog)
            await pilot.press("enter")  # accept prefilled "now"
            await pilot.pause()
            assert app.doc.running() is None
            assert len(task.clocks) == 2

            # priority + status cycling
            await pilot.press("1")
            assert task.priority == 1
            await pilot.press("t")  # IN-PROGRESS -> HOLD
            assert task.status == "HOLD"

            # undo / redo of document changes
            await pilot.press("ctrl+z")
            await pilot.pause()
            assert app.doc.projects[0].tasks[0].status == "IN-PROGRESS"
            await pilot.press("ctrl+z")
            await pilot.pause()
            assert app.doc.projects[0].tasks[0].priority == 3
            await pilot.press("ctrl+y")
            await pilot.pause()
            assert app.doc.projects[0].tasks[0].priority == 1

            # -- comments ------------------------------------------------
            task = app.doc.projects[0].tasks[0]  # undo swapped objects
            select(app, task)
            await pilot.press("m")
            assert isinstance(app.screen, CommentDialog)
            await pilot.press(*"first line", "enter", *"second", "ctrl+s")
            await pilot.pause()
            assert task.comments == ["first line", "second"]
            assert "# first line" in path.read_text(encoding="utf-8")

            # comment on a clock entry
            clock = task.clocks[0]
            select(app, clock)
            await pilot.press("m")
            await pilot.press(*"about this session", "ctrl+s")
            await pilot.pause()
            assert clock.comments == ["about this session"]

            # edit a comment line -> whole block in one editor
            select(app, task)
            await pilot.press("m")
            area = app.screen.query_one("#comment-text")
            assert area.text == "first line\nsecond"
            await pilot.press("escape")
            await pilot.pause()

            # jump to running CLOCK + collapse all
            clocks_before = len(task.clocks)
            select(app, task)
            await pilot.press("i")  # start a running clock
            await pilot.pause()
            running_clock = app.doc.running()[2]
            await pilot.press("C")  # collapse every project
            await pilot.pause()
            assert all(p.collapsed for p in app.doc.projects)
            await pilot.press("J")  # jump to the open CLOCK line
            await pilot.pause()
            assert app.selected_item() is running_clock
            assert app.doc.project_of(task).collapsed is False
            assert task.collapsed is False

            # mark DONE closes the running clock; T cycles status backward
            select(app, task)
            await pilot.press("D")
            await pilot.pause()
            assert task.status == "DONE"
            assert app.doc.running() is None
            await pilot.press("T")  # DONE -> CANCELLED (reverse)
            assert task.status == "CANCELLED"
            await pilot.press("t")  # CANCELLED -> DONE (forward)
            assert task.status == "DONE"

            # project-level DONE marks all tasks after confirmation
            await pilot.press("T")  # back to CANCELLED so the change is visible
            assert task.status == "CANCELLED"
            select(app, app.doc.projects[0])
            await pilot.press("D")
            await pilot.pause()
            await pilot.press("y")  # confirm
            await pilot.pause()
            assert task.status == "DONE"

            # restore a sensible state for later steps
            select(app, task)
            await pilot.press("T")  # -> CANCELLED
            await pilot.press("T")  # -> HOLD
            assert task.status == "HOLD"
            # drop the synthetic clock so later clock-count assertions hold
            task.clocks.pop()
            app.doc.save()
            app.rebuild_tree()
            await pilot.pause()
            assert len(task.clocks) == clocks_before

            # delete the comment block -> soft-deleted with ###
            select(app, task)
            await pilot.press("down")  # first comment line under the task
            assert isinstance(app.query_one(Tree).cursor_node.data, CommentRef)
            await pilot.press("d", "y")
            await pilot.pause()
            assert task.comments == []
            assert task.tombstones == ["### first line", "### second"]

            # delete a clock entry -> ## CLOCK line, its comments -> ###
            select(app, clock)
            await pilot.press("d", "y")
            await pilot.pause()
            assert len(task.clocks) == 1
            assert any("CLOCK:" in s and s.startswith("## ") for s in task.tombstones)
            assert "### about this session" in task.tombstones
            text = path.read_text(encoding="utf-8")
            assert "### first line" in text

            # nothing is shown for deleted lines, but undo still works
            await pilot.press("ctrl+z")
            await pilot.pause()
            task = app.doc.projects[0].tasks[0]
            assert len(task.clocks) == 2
            await pilot.press("ctrl+y")
            await pilot.pause()
            task = app.doc.projects[0].tasks[0]
            assert len(task.clocks) == 1

            # delete the whole task -> project tombstones
            select(app, task)
            await pilot.press("d", "y")
            await pilot.pause()
            project = app.doc.projects[0]
            assert project.tasks == []
            assert any(s.startswith("## ** ") for s in project.tombstones)

            # expunge removes every ## line
            await pilot.press("X", "y")
            await pilot.pause()
            assert app.doc.tombstone_count() == 0
            assert "##" not in path.read_text(encoding="utf-8")

            # consistency check opens and closes
            await pilot.press("c")
            assert isinstance(app.screen, ReportDialog)
            await pilot.press("escape")
            await pilot.pause()

            # generate a time report to a file
            await pilot.press("R")
            assert isinstance(app.screen, ReportInputDialog)
            await pilot.press("ctrl+s")  # blank dates, default filename
            await pilot.pause()
            reports = list(path.parent.glob("orgtime-report-*.txt"))
            assert reports, "report file not written"
            body = reports[0].read_text(encoding="utf-8")
            assert "orgtime time report" in body and "By day" in body

            await pilot.press("q")

        text = path.read_text(encoding="utf-8")
        doc, issues = parse(text)
        assert issues == [], issues
        assert doc.projects[0].name == "Website Redesign"
        print("PASS tui smoke test")
        print("--- saved file ---")
        print(text)


if __name__ == "__main__":
    asyncio.run(run())
