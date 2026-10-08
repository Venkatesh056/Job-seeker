"""The tool layer — what agents are allowed to do.

Importing this module is what registers the tools; `base.py` holds the machinery and the
safety invariants, and the three modules beside it hold the tools themselves.

**Toolsets, not one big pile.** A model handed thirty tools chooses worse than one handed
the four that fit its job. So each agent gets a named subset, and the subset is also a
permission boundary: the agent that writes cover letters has no way to shortlist a job,
and the one that ranks opportunities has no way to touch a browser.

The supervisor's set is the interesting one. It can look things up and queue work — it
cannot tailor a document, cannot fill a form and cannot submit anything. It decides *what
should happen next*; the specialists do the thing.
"""

from __future__ import annotations

from typing import Any

from . import actions, candidate, research  # noqa: F401  — importing registers the tools
from .base import (
    DEFAULT_TIMEOUT_S,
    MAX_RESULT_CHARS,
    REGISTRY,
    Effect,
    Tool,
    ToolError,
    ToolLog,
    ToolResult,
    invoke,
    tool,
)

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_RESULT_CHARS",
    "REGISTRY",
    "TOOLSETS",
    "Effect",
    "Tool",
    "ToolError",
    "ToolLog",
    "ToolResult",
    "describe",
    "invoke",
    "specs_for",
    "tool",
    "toolset",
]


TOOLSETS: dict[str, list[str]] = {
    # Decides what to work on next. Can queue work, cannot do the work.
    "supervisor": [
        "read_candidate_facts",
        "score_job_fit",
        "resolve_job_family",
        "read_company_research",
        "past_applications_to_company",
        "check_posting_live",
        "shortlist_job",
        "record_finding",
    ],
    # Judges one opportunity. Read-only by design: ranking must not have side effects,
    # because a ranking pass runs over hundreds of jobs.
    "opportunity": [
        "score_job_fit",
        "resolve_job_family",
        "read_company_research",
        "past_applications_to_company",
        "read_candidate_facts",
    ],
    # Writes a resume. Needs the facts and the checker, nothing else — in particular no
    # ability to look up a company, which is how unevidenced company detail gets in.
    "resume": [
        "read_candidate_facts",
        "check_claims_supported",
        "resolve_job_family",
    ],
    # Answers form questions. Question memory is here because a previously approved answer
    # beats a newly generated one.
    "answers": [
        "read_candidate_facts",
        "check_claims_supported",
        "recall_answer_to_question",
    ],
    # Writes a cover letter or an outreach message. Company research is permitted because
    # it is the *only* admissible source of company detail.
    "writing": [
        "read_candidate_facts",
        "check_claims_supported",
        "read_company_research",
    ],
    # Finds the real door into a company.
    "discovery": [
        "find_openings_at_company",
        "read_company_research",
        "check_posting_live",
        "resolve_job_family",
    ],
    # Looks at outcomes and proposes explanations. Read-only plus the note-taking tool:
    # a learning agent must never be able to act on its own conclusions.
    "learning": [
        "past_applications_to_company",
        "score_job_fit",
        "read_candidate_facts",
        "record_finding",
    ],
}


def toolset(name: str) -> list[Tool]:
    """The tools one agent may use. An unknown name gets nothing, loudly.

    Returning an empty list rather than everything is the safe direction: an agent with no
    tools degrades to its deterministic path, which is a working outcome. An agent
    accidentally handed every tool is not.
    """
    names = TOOLSETS.get(name)
    if names is None:
        from ..logging import get_logger

        get_logger("tools").warning(
            "unknown_toolset", requested=name, known=sorted(TOOLSETS), effect="no tools granted"
        )
        return []
    return REGISTRY.subset(names)


def specs_for(name: str) -> list[dict[str, Any]]:
    """Function schemas for one agent's toolset, ready to hand to a model."""
    return [t.spec() for t in toolset(name)]


def describe() -> dict[str, Any]:
    """The whole layer, for `/api/v1/tools` and for the audit.

    `writes` and `external` are listed separately because they are the answer to "what can
    this system actually do to the outside world?" — and `external` being empty is the
    interesting fact, not an omission.
    """
    tools = REGISTRY.all()
    return {
        "count": len(tools),
        "by_effect": {
            effect.value: [t.name for t in tools if t.effect is effect] for effect in Effect
        },
        "toolsets": {name: list(names) for name, names in sorted(TOOLSETS.items())},
        "tools": [t.to_dict() for t in tools],
        "absent_by_design": [
            "submit_application",
            "send_outreach",
            "write_candidate_fact",
            "solve_captcha",
            "authenticate",
            "complete_mfa",
            "run_command",
            "read_file",
            "fetch_url",
        ],
    }
