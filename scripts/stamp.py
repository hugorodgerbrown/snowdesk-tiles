"""Timestamped log lines for the tile build scripts.

Hugo, 20 Nov 2026: every logging statement carries a timestamp, so a pasted
log says when each step ran. ``log`` is ``print`` to stderr with the local
date and time in front, in the same shape as ``log`` in scripts/log.sh:

    2026-11-20 12:44:31 ==> classifying 184832x103680 cells

stdout stays for data the shell scripts read (extents, heights, ok/FAIL
results), and is never stamped here; vm-build.sh stamps those lines as it
shows them.
"""

from __future__ import annotations

import sys
import time

#: The stamp's format: local time, to the second, with the date, because a
#: terrain run spans days.
STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def stamp() -> str:
    """Return the current local time in ``STAMP_FORMAT``."""
    return time.strftime(STAMP_FORMAT)


def log(*values: object, sep: str = " ", end: str = "\n") -> None:
    """Print ``values`` to stderr after a timestamp, flushed at once."""
    print(stamp(), *values, sep=sep, end=end, file=sys.stderr, flush=True)
