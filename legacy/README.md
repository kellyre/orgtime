# legacy/ — frozen code

Code kept for reference but **no longer maintained**. New features go to the
curses front-end in [`../orgtime/`](../orgtime) only.

## textual_app.py — the Textual TUI (frozen 2026-06-12)

The original front-end, built with [Textual](https://textual.textualize.io/).
The curses app (`orgtime/curses_app.py`) is now the canonical application;
this one is kept as a working snapshot and is not updated with new features.

It reuses the shared, UI-agnostic core in `orgtime/` (`model`, `view`,
`report`) via absolute imports, so it keeps working as long as those modules'
interfaces don't change. If a shared change ever breaks it, that's expected —
it won't be fixed unless the Textual version is revived.

Run it (needs `textual`: `pip install -r legacy/requirements.txt`):

```
python -m legacy.textual_app [path/to/file.org]
```

Its smoke test (also frozen, not in the routine suite):

```
PYTHONPATH=. python legacy/test_textual_app.py
```
