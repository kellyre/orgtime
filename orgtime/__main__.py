"""Entry point: python -m orgtime [file]

Launches the curses front-end (the maintained app). The frozen Textual
version lives in legacy/ and is run with: python -m legacy.textual_app
"""

from .curses_app import main

if __name__ == "__main__":
    main()
