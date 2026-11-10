@echo off
rem Launch the frozen curses version of orgtime with the project venv.
rem Optional arg: path to a .org file (defaults to timelog.org).
rem Requires windows-curses:  venv\Scripts\pip install -r legacy\requirements.txt
"%~dp0venv\Scripts\python.exe" -m legacy.curses_app %*
