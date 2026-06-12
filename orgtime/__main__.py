"""Entry point: python -m orgtime [file]"""

import sys
from pathlib import Path

from .app import OrgTimeApp


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("timelog.org")
    OrgTimeApp(path.resolve()).run()


if __name__ == "__main__":
    main()
