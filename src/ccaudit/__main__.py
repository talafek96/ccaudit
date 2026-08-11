"""``python -m ccaudit`` — the same entry point as the ``ccaudit`` console script."""

import sys

from ccaudit.cli import main

if __name__ == "__main__":
    sys.exit(main())
