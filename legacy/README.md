# legacy/ — frozen code

Code kept for reference but **no longer maintained**. New features go to the
Textual front-end in [`../orgtime/`](../orgtime) only.

## curses_app.py — the curses TUI (frozen 2026-11-09)

A dependency-free front-end, built with the standard library's `curses`
(plus `windows-curses` on Windows). It was the canonical application from
2026-06-12 until the target deployment environment flipped to having
Textual available but not `windows-curses`; the Textual app
(`orgtime/textual_app.py`) is now canonical, and this one is kept as a
working snapshot for environments without Textual.

It reuses the shared, UI-agnostic core in `orgtime/` (`model`, `view`,
`report`, `config`, `calimport`, `notes`) via absolute imports, so it keeps
working as long as those modules' interfaces don't change. If a shared
change ever breaks it, that's expected — it won't be fixed unless the
curses version is revived.

Run it (needs `windows-curses` on Windows: `pip install -r legacy/requirements.txt`):

```
python -m legacy.curses_app [path/to/file.org]
```

Its end-to-end test (also frozen, not in the routine suite):

```
PYTHONPATH=. python legacy/test_curses_headless.py
```
