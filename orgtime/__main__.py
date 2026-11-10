"""Entry point: python -m orgtime [file]

Launches the Textual front-end (the maintained app). The frozen curses
version lives in legacy/ and is run with: python -m legacy.curses_app
"""

from .textual_app import main

if __name__ == "__main__":
    main()
