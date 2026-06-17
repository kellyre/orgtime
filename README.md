# orgtime

A simple terminal time tracker inspired by Emacs org-mode clocking.
Projects contain tasks; tasks accumulate `CLOCK:` entries. Everything is
stored in a plain-text, org-like file you can edit by hand.

The app is a **curses** terminal UI. `curses` ships with Python on
Linux/macOS (no third-party packages at all); on Windows it needs the
`windows-curses` shim. The UI-agnostic core (`model`, `view`, `report`) is
kept separate so it can back other front-ends.

> A Textual front-end also exists but is **frozen / unmaintained** — see
> [`legacy/`](legacy). New features go to the curses app only.

## Setup

```
git clone https://github.com/kellyre/orgtime.git
cd orgtime
python -m venv venv
```

On Linux/macOS no dependencies are required. On Windows, install the curses
shim once:

```
venv\Scripts\pip install -r requirements.txt
```

## Run

```
python -m orgtime                 # opens/creates timelog.org here
python -m orgtime path/to/file.org
```

On Windows you can use the launcher `orgtime.bat [file]` instead.

Press **`?`** inside the app for the full key list.

## Keys

| Key       | Action                                                        |
|-----------|---------------------------------------------------------------|
| `n`       | New task (in the selected project)                            |
| `N`       | New project                                                   |
| `e`       | Edit selected — rename project/task, or edit a clock / comment |
| `d`       | Delete selected (soft, with confirmation)                     |
| `c`       | Add/edit the comment below the selected line (multi-line)     |
| `m`       | Move the selected task to a different project                  |
| `i` / `o` | Clock in / out now (clock-in clocks out anything running)     |
| `I` / `O` | Clock in / out at a time you type (late starts, forgotten stops) |
| `s` / `S` | Scroll task status forward / backward through the cycle        |
| `D`       | Mark task DONE (closes a running clock); on a project, mark all tasks DONE (after confirmation) |
| `1`–`5`   | Set priority of selected project/task (1 = highest)           |
| `u` / `U` | Undo / redo last change                                       |
| `r`       | Generate a time report to a text file (prompts for date range)|
| `L`       | Reload file from disk (after hand-editing)                    |
| `v`       | Verify: consistency check (format + semantic problems)        |
| `X`       | Expunge: permanently remove all soft-deleted (`##`) lines     |
| `J`       | Jump to the running clock entry (expands its project and task) |
| `C`       | Collapse all projects                                        |
| `space` / `enter` / `tab` | Collapse / expand the selected project or task |
| `/`       | Search project/task names and comments; press `/` again to jump to the next match (loops); Esc cancels, leaving you on the current match |
| `?`       | Show the in-app key list                                     |
| `q`       | Save and quit                                                 |

Arrow keys / `j` `k` move the cursor; `g` / `G` jump to top / bottom. Every
change is saved to the file immediately.

In the multi-line comment editor (`c`): **`Ctrl+O`** saves, **Esc** cancels,
Enter inserts a newline.

In single-line prompts (name, search, times): type and press `Enter`; `Esc`
cancels. New projects/tasks default to priority `#3` and status `TODO` —
adjust afterward with `1`–`5` and `s`/`S`/`D`. Time fields accept
`YYYY-MM-DD HH:MM` or just `HH:MM` (today's date is assumed; for clock-out,
the day the clock started). To fix an already-recorded interval, expand the
task and press `e` on the CLOCK line.

### Overlap resolution

Because you can only do one thing at a time, two clock entries should never
overlap. When editing a CLOCK line creates an overlap with any entry in any
task or project, the app lists every affected entry and the change it would
make, then applies them on confirmation (Esc/No cancels the whole edit):

- an entry overlapping on one side is trimmed back to the edge;
- an entry that fully surrounds the new interval is split into two;
- an entry entirely covered collapses to a zero-length (`0:00`) slot — you're
  told this will happen and it proceeds only if you confirm.

## File format

```
* [#2] Project name
# comment attached to the project
** TODO [#1] Task name
# comment attached to the task
   CLOCK: [2026-06-09 Tue 09:00]--[2026-06-09 Tue 10:30] => 1:30
# comment attached to that clock entry
   CLOCK: [2026-06-10 Wed 11:00]
## ** TODO a soft-deleted task line
### a soft-deleted comment line
```

- `[#N]` is priority 1–5 (1 highest).
- Task status is one of `TODO`, `IN-PROGRESS`, `HOLD`, `CANCELLED`, `DONE`.
- A `CLOCK:` line without an end timestamp is a running clock.
- Legacy `:DESCRIPTION:` lines from older files are migrated to comments on
  load (descriptions are no longer a separate feature).
- A line starting with a single `#` is a comment, attached to the nearest
  project/task/clock line above it. In the app, comments show under their
  owner and collapse with it; `c` adds or edits the block, and adjoining
  comment lines are always edited as one block.

### Time reports

Press `r` to write a plain-text time report. You're prompted for a start and
end date (`YYYY-MM-DD`, blank = open-ended) and an output filename
(defaulting to `orgtime-report-<start>_<end>.txt` next to your `.org` file).
The report shows a grand total plus three groupings — by project, by project
and task, and by day. A clock entry is counted, in full, on the date of its
start timestamp; running clocks are counted up to generation time and listed
in a note.

### Soft deletion

Deleting anything in the app prepends `##` to its lines instead of removing
them (a deleted `# comment` therefore becomes `###`). Lines starting with
two or more `#` are invisible in the app but stay in the file, so nothing
is ever lost: hand-remove the `##` and press `r` to resurrect something,
or press `X` to expunge all soft-deleted lines for good.

Hand-edit freely, then press `L` to reload in the app. Malformed lines are
reported at load time; `v` (verify) additionally flags semantic problems:
stated duration not matching the timestamps, day name not matching the date,
end before start,
overlapping clock entries, more than one running clock, and running clocks
on DONE/CANCELLED tasks. It also flags implausible entries — timestamps in
the future, closed entries of 24 hours or more, and clocks left running for
more than a day; these get a red ⚠ in the tree and a warning toast when
created. Durations in `CLOCK:` lines are always hours:minutes (`48:00` =
48 hours), but task/project totals in the tree switch to day form
(`2d 22:43`) once they pass 24 hours.

## Tests

Each file is a plain script (no pytest). Run from the repo root:

```
PYTHONPATH=. venv\Scripts\python tests\test_model.py
PYTHONPATH=. venv\Scripts\python tests\test_view.py
PYTHONPATH=. venv\Scripts\python tests\test_report.py
PYTHONPATH=. venv\Scripts\python tests\test_overlap.py
PYTHONPATH=. venv\Scripts\python tests\test_curses_headless.py
```

`test_curses_headless.py` drives the real curses UI (needs `windows-curses`
on Windows). The frozen Textual test lives in `legacy/` and is not part of
this suite.
