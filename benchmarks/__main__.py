"""``python -m benchmarks`` → run the suite (alias for ``benchmarks.run``)."""

from __future__ import annotations

import sys

from benchmarks.run import main

if __name__ == "__main__":
    sys.exit(main())
