# orgtime

A simple terminal time tracker inspired by Emacs org-mode clocking.
Projects contain tasks; tasks accumulate `CLOCK:` entries. Everything is
stored in a plain-text, org-like file you can edit by hand.

Two front-ends share the same file format and model:

- **Textual** (`python -m orgtime`) — the full-featured UI; needs the
  `textual` package.
- **curses** (`python -m orgtime.curses_app`) — an alternative for
  locked-down environments where `textual` isn't available. Same
  functionality, plainer presentation. `curses` ships with Python on
  Linux/macOS (no third-party packages at all); on Windows it needs the
  `windows-curses` shim (`pip install -r requirements-curses.txt`).

## Setup

```
git clone https://github.com/kellyre/orgtime.git
cd orgtime
python -m venv venv
```

Windows:
```
venv\Scripts\pip install -r requirements.txt
```
Mac/Linux:
```
venv/bin/pip install -r requirements.txt
```

## Run

Windows:
```
orgtime.bat
orgtime.bat path\to\file.org
```

Mac/Linux:
```
venv/bin/python -m orgtime
venv/bin/python -m orgtime path/to/file.org
```

Without a file argument, opens (or creates) `timelog.org` in the current directory.

### curses front-end (for environments without `textual`)

On Linux/macOS, skip the install step entirely — just run:

```
python3 -m orgtime.curses_app
python3 -m orgtime.curses_app path/to/file.org
```

(or `./orgtime-curses [file]`).

On Windows, install the curses shim once, then use the launcher:

```
venv\Scripts\pip install -r requirements-curses.txt
orgtime-curses.bat
orgtime-curses.bat path\to\file.org
```

Keys match the table below, with two differences forced by the terminal:
**`u`** undoes and **`Ctrl+R`** redoes (terminals reserve `Ctrl+Z`), and
multi-line editors (description, comments) save with **`Ctrl+G`**. Press
**`?`** inside the app for the full key list.

## Keys

| Key       | Action                                                        |
|-----------|---------------------------------------------------------------|
| `N`       | New project                                                   |
| `n`       | New task (in the selected project)                            |
| `e`       | Edit selected project / task / clock entry                    |
| `d`       | Delete selected (with confirmation)                           |
| `i`       | Clock in on the selected task (clocks out anything running)   |
| `o`       | Clock out now                                                 |
| `I`       | Clock in at a time you type (for late starts)                 |
| `O`       | Clock out at a time you type (for forgotten clock-outs)       |
| `t`       | Cycle task status (TODO → IN-PROGRESS → HOLD → CANCELLED → DONE) |
| `m`       | Add/edit a comment below the selected line (multi-line)       |
| `1`–`5`   | Set priority of selected project/task (1 = highest)           |
| `ctrl+z` / `u` | Undo last change (`ctrl+y` redoes)                       |
| `R`       | Generate a time report to a text file (prompts for date range)|
| `X`       | Expunge: permanently remove all soft-deleted (`##`) lines     |
| `space` / `enter` | Collapse / expand the selected project or task        |
| `c`       | Consistency check (shows format + semantic problems)          |
| `r`       | Reload file from disk (after hand-editing)                    |
| `q`       | Save and quit                                                 |

Arrow keys / `j` `k` move the cursor. The bar above the footer shows the
running clock, ticking live. Every change is saved to the file immediately.

In dialogs: `Enter` on the name field saves right away, `ctrl+s` saves from
any field, `Esc` cancels. Text fields open with the cursor at the end (no
select-all), and `ctrl+z` inside a field undoes your keystrokes there. Priority and status are radio rows — tab to them
and pick with the arrow keys. Time fields accept `YYYY-MM-DD HH:MM` or just
`HH:MM` (today's date is assumed; for clock-out, the day the clock started).
To fix an already-recorded interval, expand the task and press `e` on the
CLOCK line.

## File format

```
* [#2] Project name
  :DESCRIPTION: free text
# comment attached to the project
** TODO [#1] Task name
   :DESCRIPTION: free text
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
- Multiple `:DESCRIPTION:` lines become a multi-line description.
- A line starting with a single `#` is a comment, attached to the nearest
  project/task/clock line above it. In the app, comments show under their
  owner and collapse with it; `m` adds or edits the block, and adjoining
  comment lines are always edited as one block.

### Time reports

Press `R` to write a plain-text time report. You're prompted for a start and
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

Hand-edit freely, then press `r` in the app. Malformed lines are reported
at load time; `c` additionally flags semantic problems: stated duration not
matching the timestamps, day name not matching the date, end before start,
overlapping clock entries, more than one running clock, and running clocks
on DONE/CANCELLED tasks. It also flags implausible entries — timestamps in
the future, closed entries of 24 hours or more, and clocks left running for
more than a day; these get a red ⚠ in the tree and a warning toast when
created. Durations in `CLOCK:` lines are always hours:minutes (`48:00` =
48 hours), but task/project totals in the tree switch to day form
(`2d 22:43`) once they pass 24 hours.

## Tests

```
PYTHONPATH=. venv\Scripts\python tests\test_model.py
PYTHONPATH=. venv\Scripts\python tests\test_app.py
```
