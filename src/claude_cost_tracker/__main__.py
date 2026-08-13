"""``python -m claude_cost_tracker`` — the same entry point as the ``ccost`` console script."""

import sys

from claude_cost_tracker.cli import main

if __name__ == "__main__":
    sys.exit(main())
