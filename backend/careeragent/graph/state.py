"""Shared state for the LangGraph campaigns.

Every field that a fan-out writes to carries an `add` reducer. That is not a detail —
it is the whole reason the graph is worth having here. Twelve sourcing agents run
concurrently and all of them append to `hits`; without a reducer LangGraph raises
`InvalidUpdateError` on the concurrent write, and with a hand-rolled `asyncio.gather`
the merge would be the caller's problem to get right every time.

State is deliberately plain: dataclasses and lists of dicts, nothing that holds a
socket or a browser page. A checkpointer has to be able to serialize all of it, and the
one thing that genuinely cannot be serialized — the live browser page held across an
approval — stays outside the graph in `applications/sessions.py`.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


def _merge_counts(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    """Sum per-source call counts across concurrent branches.

    Needed because quota accounting is the thing a free tier punishes you for getting
    wrong, and each parallel branch only knows about its own calls.
    """
    out = dict(left)
    for key, value in (right or {}).items():
        out[key] = out.get(key, 0) + value
    return out


class CampaignState(TypedDict, total=False):
    """Discovery: resume + region + families → ranked, applyable companies."""

    # ─── Inputs ─────────────────────────────────────────────────────────────
    run_id: str
    candidate_id: str
    locations: list[str]
    family_ids: list[str]
    include_related: bool
    max_age_days: int
    max_companies: int
    enrich_limit: int

    # ─── Profile agent ──────────────────────────────────────────────────────
    detected_field: str
    archetype: str
    resolved_families: list[dict[str, Any]]
    queries: list[str]

    # ─── Sourcing agents (parallel; every one of these appends) ─────────────
    hits: Annotated[list[dict[str, Any]], operator.add]
    source_calls: Annotated[dict[str, int], _merge_counts]
    source_errors: Annotated[list[str], operator.add]
    exhausted: Annotated[list[str], operator.add]

    # ─── Aggregation ────────────────────────────────────────────────────────
    companies: list[dict[str, Any]]

    # ─── Enrichment agents (parallel) ───────────────────────────────────────
    enriched: Annotated[list[dict[str, Any]], operator.add]

    # ─── Match and rank ─────────────────────────────────────────────────────
    jobs: list[dict[str, Any]]
    ranked: list[dict[str, Any]]
    stats: dict[str, Any]
    error: str


class SourcingTask(TypedDict):
    """One (query, location, source) unit of work, fanned out with Send()."""

    query: str
    location: str
    source_id: str
    family_id: str
    max_age_days: int


class EnrichTask(TypedDict):
    """One company to resolve an ATS board for."""

    company: dict[str, Any]
    candidate_id: str
    fetch_jobs: bool


class ApplyState(TypedDict, total=False):
    """One application, start to finish.

    Separate from `CampaignState` because it has a completely different lifetime: a
    campaign runs for minutes, an application can sit at an approval interrupt for days.
    """

    application_id: str
    candidate_id: str
    job_id: str
    autonomy_level: int
    mode: str  # one_click | preview | threshold
    use_llm: bool

    resume_render_id: str
    cover_letter_id: str
    validation: dict[str, Any]

    ats: str
    url: str
    filled: list[dict[str, Any]]
    needs_user: list[str]
    never_filled: list[str]
    holds: list[str]
    answers: list[dict[str, Any]]
    screenshots: list[str]

    approved: bool
    field_overrides: dict[str, str]
    outcome: str
    status: str
    evidence: dict[str, Any]
    error: str


class SupervisorState(TypedDict, total=False):
    """The supervisor's working memory — spec §5.

    Different in kind from the other two. `CampaignState` and `ApplyState` describe a
    pipeline whose shape is known before it starts; this describes a conversation with
    itself, where the next step is chosen from what the last one found.

    So the fields that matter are the ones a *router* reads: what was asked, what has been
    done, what is outstanding, and how many times we have gone round. `history` and
    `plan` carry `add` reducers because a step may fan out, and because losing a step
    would make the trail a lie about what happened.
    """

    # ─── The request ────────────────────────────────────────────────────────
    run_id: str
    candidate_id: str
    goal: str
    autonomy_level: int
    max_steps: int
    dry_run: bool

    # ─── What the supervisor decided to do ──────────────────────────────────
    plan: Annotated[list[dict[str, Any]], operator.add]
    next_step: str
    step_reason: str
    step_input: dict[str, Any]

    # ─── What happened ──────────────────────────────────────────────────────
    history: Annotated[list[dict[str, Any]], operator.add]
    findings: Annotated[list[str], operator.add]
    # Summed, not assigned. Each step returns 1 and LangGraph adds it; a node returning an
    # absolute value would race with any future fan-out and, worse, a node that forgot to
    # return it at all would silently stop the counter — which is how `max_steps` becomes
    # decorative and the run only ends at the recursion limit.
    steps_taken: Annotated[int, operator.add]

    # ─── Outcomes worth surfacing ───────────────────────────────────────────
    shortlisted: Annotated[list[dict[str, Any]], operator.add]
    prepared: Annotated[list[dict[str, Any]], operator.add]
    awaiting_approval: Annotated[list[str], operator.add]

    # ─── Termination ────────────────────────────────────────────────────────
    done: bool
    stopped_because: str
    summary: str
    error: str
