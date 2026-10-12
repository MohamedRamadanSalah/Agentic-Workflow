#!/usr/bin/env python3
"""Bug Assurance controller entry point: ``python ba.py <command> [options]``.

Run ``python ba.py --help`` for the command list. Standard library only (Python 3.9+).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bug_assurance.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
