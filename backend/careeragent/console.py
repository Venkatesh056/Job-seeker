"""Console output setup.

Lives in its own module so the UTF-8 reconfigure runs at import time, before
anything writes, without leaving `cli.py` with imports below executable code.
"""

from __future__ import annotations

import contextlib
import sys

from rich.console import Console


def _force_utf8_stdout() -> None:
    """Windows consoles still default to cp1252, which cannot encode the arrows
    and dashes in this output — it raises UnicodeEncodeError mid-render rather
    than degrading. Reconfiguring is cheap and makes the CLI behave the same
    everywhere.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # A redirected or already-wrapped stream; not worth failing over.
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


_force_utf8_stdout()

console = Console()
