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
| `H`       | Find small overlaps bordering a half-hour and snap them to it |
| `J`       | Jump to the running clock entry (expands its project and task) |
| `C`       | Collapse all projects                                        |
| `z`       | Cycle project sort: file → priority → created → modified (view only) |
| `A`       | Import an Outlook calendar CSV export as clock entries         |
| `t`       | Timeline mode: one day's workday window with gaps, to fill gaps (in timeline: `<`/`>` widen, `R` reset, `W` save as default) |
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

A task's CLOCK entries are displayed most-recent-first, so the entry you're
most likely to want — today's — is always right under the task line. This
is display order only; the file itself still stores entries chronologically,
so hand-editing stays natural.

### Overlap resolution

Because you can only do one thing at a time, two clock entries should never
overlap. When editing a CLOCK line creates an overlap with any entry in any
task or project, a popup lists the conflict and asks **whose time takes
precedence** (`Esc` cancels the whole edit):

- **`e` — the edited time wins**: the other entries are adjusted. An entry
  overlapping on one side is trimmed to the edge; an entry that fully
  surrounds the edit is split in two; an entry entirely covered is wiped to
  `0:00`.
- **`o` — the other entries win**: they stay put and the edited time is
  trimmed (or split, if an entry sits inside it) to fit the gaps; if the edit
  is fully covered it is wiped to `0:00`.

Either way, the popup flags any entry that would be **wiped out to `0:00`**,
since that usually means a mistyped time.

### Half-hour snap

A very small overlap — under 10 minutes, bordering or straddling an exact
half-hour (`:00` or `:30`) — is usually just clock-in/out imprecision, not a
real double-booking. Press `H` at any time to scan the whole file for these
and, after a single confirmation, snap **both** boundaries to that half-hour
mark, closing the gap. `H` finds nothing if there's nothing to fix, and the
consistency check (`v`) mentions the count if any exist. Overlaps of 10
minutes or more, or ones that don't land on a half-hour, are left for the
regular overlap-resolution popup above.

## Staleness colouring

Projects and tasks that haven't been touched in a while are dimmed so old
work doesn't visually compete with what you're actively doing:

- A **task** untouched for 7+ days is shown muted (dim green).
- A **project** whose most recent task activity is 14+ days old is shown in
  a darker shade of the same colour.
- Either one untouched for 90+ days is shown as a shade of gray, regardless
  of the shorter thresholds above.

"Touched" is the same `modified` timestamp used for created/modified times
and sorting (see below) — so clocking in, commenting, changing status, etc.
all count and reset the staleness clock.

## File format

```
* [#2] Project name
  :CREATED: [2026-06-01 Mon 09:00] :MODIFIED: [2026-06-18 Thu 12:23]
# comment attached to the project
** TODO [#1] Task name
   :CREATED: [2026-06-01 Mon 09:30] :MODIFIED: [2026-06-18 Thu 12:23]
# comment attached to the task
   CLOCK: [2026-06-09 Tue 09:00]--[2026-06-09 Tue 10:30] => 1:30
# comment attached to that clock entry
   CLOCK: [2026-06-10 Wed 11:00]
## ** TODO a soft-deleted task line
### a soft-deleted comment line
```

- `[#N]` is priority 1–5 (1 highest).
- Task status is one of `TODO`, `IN-PROGRESS`, `HOLD`, `CANCELLED`, `DONE`.
- The `:CREATED:`/`:MODIFIED:` line (optional, auto-filled on load) tracks
  when a project/task was made and last changed — see *Created / modified
  times* below.
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

### Created / modified times

Each project and task carries a created and a modified timestamp, stored on a
line under its header:

```
* [#2] Project name
  :CREATED: [2026-06-01 Mon 09:00] :MODIFIED: [2026-06-18 Thu 12:23]
```

- On load, any project/task missing these defaults to its most recent clock
  entry (for a project, across all its tasks), or the current time if it has
  no clocks — so older files just work.
- `modified` is bumped automatically when you create a task, or touch a clock,
  comment, status, priority, name, or move (the change bubbles up to the
  owning project too).
- Both are plain text you can hand-edit. `e` on a project/task also lets you
  fix `created` (e.g. for something created late) — keep the name with Enter,
  then type a new created time.
- `z` cycles the project sort (file → priority → created → modified). It's a
  view-only sort: your file order on disk is untouched, and it resets to file
  order on restart. When sorting by created/modified, that timestamp is shown
  on each project line.

### Timeline mode

Press `t` for a full-screen timeline of a single day's workday window
(7am–6pm by default), laid out chronologically as your clock entries
interleaved with the **gaps** between them (gaps are highlighted). A header
shows the day and how much was worked vs. free.

- Up/Down (or `j`/`k`) move the cursor; `[` / `]` step to the previous/next
  day; `g` jumps to a date; `u` undoes; `q`/`Esc` returns to the tree.
- Select a **gap** and press `a` (or Enter) to add an entry in it: type the
  start/end (defaulting to the whole gap, and constrained to stay inside it),
  then pick a project and task (or make new ones). The gap shrinks, so you
  can keep adding more entries into what's left.
- The window isn't a hard limit: `<` / `>` widen it earlier / later, one hour
  at a time, for however far your day actually ran. If entries exist outside
  the current window, a hint (e.g. `+1 earlier (< to expand)`) tells you
  they're there. `R` resets the window back to the default for this session;
  `W` saves the *current* window as the new default going forward.
- The default window is stored in a small hand-editable settings file,
  `orgtime.cfg`, next to your `.org` file (created the first time you press
  `W`; edit it directly if you prefer — `workday_start = 7` /
  `workday_end = 18`).

### Calendar import

Press `A` to import clock entries from an Outlook calendar CSV export. You're
prompted for the CSV path, the **blackout status code** (`Show time as` value
that means Out of Office — `4` on legacy tenants, `3` on newer ones), and a
start/end date range (blank = the full span of the file). Only entries whose
**start date** is in range are considered.

The importer reads the `Subject`, `Start/End Date/Time`, `All day event`, and
`Show time as` columns (matched by name, extra columns ignored):

- **Blackout windows** are computed first from every event with the blackout
  status. Any other event overlapping a blackout window is silently ignored
  (e.g. a meeting during an all-day "Out of Office" trip).
- **All-day events are never imported** as clocks — they only serve as
  blackout windows.
- Each remaining candidate is offered **most-recent-first** (newest date/time
  down to oldest), with its subject, time, and status; you choose: **k** keep,
  **a** all (import every entry with this subject to the same project/task),
  **s** skip, **i** ignore all with this subject, or **q** quit. Processing
  newest-first means that if you get interrupted partway through, the recent
  entries — the ones you're most likely to need next — are already done.
  `all`/`ignore all` apply to the remainder in this same newest-to-oldest
  order.
- **keep**/**all** then ask which project and task to import into (or make a
  new one, defaulting the task name to the subject).
- An entry that exactly matches an existing clock is auto-skipped, so
  re-importing the same range doesn't create duplicates.
- If an import overlaps an existing entry, the same precedence popup as time
  editing appears (`e` imported wins, `o` existing wins, `Esc` skip this one).

### Backups

The file is copied to `backups/<name>_<YYYYMMDD-HHMMSS>.org` each time the app
starts and again when an import begins, so you can roll back. (Cleanup of old
backups is up to you for now.)

### Soft deletion

Deleting anything in the app prepends `##` to its lines instead of removing
them (a deleted `# comment` therefore becomes `###`). Lines starting with
two or more `#` are invisible in the app but stay in the file, so nothing
is ever lost: hand-remove the `##` and press `L` to resurrect something,
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
