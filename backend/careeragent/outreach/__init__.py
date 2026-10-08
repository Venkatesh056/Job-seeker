"""Outreach transports.

The decision to contact someone lives in `applications.outreach`: it holds the approval
state, the permitted-source rule for contacts, and the rate limits. This package holds
only the means of delivery, kept separate so that "may we send this?" and "how is a
message sent?" cannot be confused for one another.
"""

from . import gmail

__all__ = ["gmail"]
