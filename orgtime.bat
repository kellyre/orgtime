@echo off
rem Launch orgtime with the project venv. Optional arg: path to .org file.
"%~dp0venv\Scripts\python.exe" -m orgtime %*
