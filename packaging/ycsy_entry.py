"""Entry point used when freezing the app into a single executable.

PyInstaller needs a real script (not ``-m ycsy``), so this just calls the CLI.
"""

import sys

from ycsy.cli import main

if __name__ == "__main__":
    sys.exit(main())
