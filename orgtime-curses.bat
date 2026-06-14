@echo off
rem Launch the curses version of orgtime with the project venv.
rem Optional arg: path to a .org file (defaults to timelog.org).
rem Requires windows-curses:  venv\Scripts\pip install -r requirements-curses.txt
"%~dp0venv\Scripts\python.exe" -m orgtime.curses_app %*
