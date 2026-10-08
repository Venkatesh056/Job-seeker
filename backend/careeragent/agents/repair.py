"""Answering a validator — the second half of the hybrid pattern.

*Agent decides, pure function vetoes* is where this started. In practice a veto usually
lands on one clause of an otherwise good result: a summary claiming "2 years" against facts
supporting 0.2, an answer citing five facts rejected for the phrase "data visualization",
a letter that is fine except for a sentence of invented enthusiasm. Discarding the whole
thing is safe and wasteful in equal measure.

So the pattern gains a step: **veto → say exactly what failed → let the agent revise once →
veto again.** The pure check still decides, and a second failure lands exactly where the
first one used to.

**One instruction here is safety-critical**, and it is the reason this is a shared module
rather than three similar functions. A model asked to fix an evidence violation will
otherwise go looking for wording that sounds supported — it will argue, hedge, or swap one
unevidenced claim for another. `REVISION_RULES` says *remove it*, and says so once. Three
copies of that instruction would be three chances for one of them to soften, which is the
same argument that put the anti-fabrication rules in a single block.

**One round.** A second costs the same again to fix something the model has already shown it
cannot fix, and an agent arguing with a validator is not worth paying for.
"""

from __future__ import annotations

from typing import Any

from ..logging import get_logger

log = get_logger("agents.repair")

MAX_ROUNDS = 1

REVISION_RULES = """Fix this by REMOVING or WEAKENING the unsupported claim.

- Do not argue that it was supported. The check has already decided, and it decides again
  after you reply.
- Do not substitute a different unsupported claim for the one you are removing.
- Keep everything the facts do back. Only the rejected part should change.
- If nothing survives removal, say so plainly and cite nothing. That is a correct answer."""


def build_prompt(
    *,
    complaint: str,
    original: str,
    facts: str,
    task: str,
    output_shape: str,
    extra: str = "",
) -> str:
    """The revision prompt. Shared so the rules above cannot drift between agents.

    `complaint` is handed over verbatim — "mentions kubernetes with no evidence" and "claims
    7 years; facts support 0.2" are specific enough to act on, and a summarised complaint
    produces a vaguer rewrite.
    """
    parts = [
        f"Your {task} was rejected by the evidence check.",
        "",
        f"THE PROBLEM:\n{complaint.strip()}",
        "",
        REVISION_RULES,
        "",
        f"WHAT YOU WROTE:\n{original.strip() or '(nothing)'}",
        "",
        f"CANDIDATE FACTS (the only permitted source):\n{facts.strip() or '(none)'}",
    ]
    if extra:
        parts += ["", extra.strip()]
    parts += ["", f"Return JSON:\n{output_shape.strip()}"]
    return "\n".join(parts)


def revise(
    *,
    agent: str,
    system: str,
    complaint: str,
    original: str,
    facts: str,
    task: str,
    output_shape: str,
    router: Any,
    visible_tokens: int,
    extra: str = "",
) -> dict[str, Any] | None:
    """Ask once for a corrected result. Returns the parsed reply, or None.

    None covers every way this can fail — no model, an unusable reply, a refusal — because
    the caller's response to all of them is identical: keep the original rejection and hand
    it to the user.
    """
    from ..llm.router import Tier, tokens_for

    prompt = build_prompt(
        complaint=complaint,
        original=original,
        facts=facts,
        task=task,
        output_shape=output_shape,
        extra=extra,
    )
    data = router.complete_json(
        prompt,
        tier=Tier.STRONG,
        # Zero: this is a correction against a stated rule, not a creative attempt. A second
        # sample of the same temperature that produced the violation is not what is wanted.
        temperature=0.0,
        system=system,
        max_tokens=tokens_for(visible_tokens),
        agent=f"{agent}_repair",
    )
    if not isinstance(data, dict):
        log.debug("repair_unusable", agent=agent)
        return None
    return data


def cited_text(data: dict[str, Any], key: str, limit: int) -> tuple[str, list[str]] | None:
    """Pull a (text, fact ids) pair out of a revision reply, or None if it is not usable.

    An uncited revision is refused rather than accepted: the agent was asked to keep what
    the facts back, and returning nothing to cite means it did not.
    """
    text = " ".join(str(data.get(key) or "").split())[:limit]
    cited = [str(c) for c in (data.get("cite") or []) if str(c).strip()]
    if not text or not cited or not bool(data.get("confident", True)):
        return None
    return text, cited
