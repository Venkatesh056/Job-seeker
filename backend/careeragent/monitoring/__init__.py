"""Career monitoring — addendum §13.

Discovery is a one-shot; this is the loop that keeps its results true. It re-reads the job
boards already resolved, notices what is new, notices what has gone, and scores the new
arrivals deterministically.

    from careeragent.monitoring import sweep, start, status

Each board carries its own cadence, halved when it produces something and lengthened when
it does not, so a company that posts weekly is checked often and one that posts yearly is
left alone. Nothing here applies to anything — it finds and tells you.
"""

from .monitor import (
    MAX_FAILURES,
    BoardResult,
    SweepReport,
    check_board,
    due_boards,
    next_interval,
    running,
    start,
    status,
    stop,
    sweep,
)

__all__ = [
    "MAX_FAILURES",
    "BoardResult",
    "SweepReport",
    "check_board",
    "due_boards",
    "next_interval",
    "running",
    "start",
    "status",
    "stop",
    "sweep",
]
