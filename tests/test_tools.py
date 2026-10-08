"""The tool layer — spec §12.

Most of this file tests absences. That is unusual and it is the point: the security
properties of an agentic system are not "the prompt says no", they are "the capability does
not exist". A test that asserts `submit_application` is not a tool will fail the day
somebody adds one for convenience, which is exactly when someone should have to think about
it.

The rest tests the boundary itself — argument validation, the approval gate, the failure
taxonomy — because an agent's most common behaviour is calling a tool slightly wrong, and
how that is handled decides whether a tool-using agent is better than a fixed pipeline or
worse.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from careeragent import tools
from careeragent.tools.base import Effect, Registry, Tool, ToolError, ToolFailure, ToolLog

# ─── What must never exist ──────────────────────────────────────────────────

# Each of these would let a model take an action that cannot be undone, or manufacture the
# evidence its own output is checked against.
FORBIDDEN = [
    "submit_application",
    "submit",
    "send_outreach",
    "send_email",
    "send_message",
    "write_candidate_fact",
    "add_fact",
    "edit_candidate",
    "update_profile",
    "solve_captcha",
    "bypass_captcha",
    "authenticate",
    "login",
    "complete_mfa",
    "run_command",
    "shell",
    "exec",
    "read_file",
    "write_file",
    "fetch_url",
    "http_get",
    "browse",
]


@pytest.mark.parametrize("name", FORBIDDEN)
def test_the_dangerous_capability_does_not_exist(name):
    """A missing tool is a wall; a prompt instruction is a request.

    If you are here because this test failed, the question is not "how do I make the test
    pass" — it is whether a model should be able to do this without a human in front of it.
    """
    assert tools.REGISTRY.get(name) is None, (
        f"{name!r} is now a tool. Submitting, sending, authenticating and writing to the "
        "Fact Bank must stay outside the tool layer."
    )


def test_nothing_reaches_the_outside_world_unattended():
    """No EXTERNAL tool exists at all today, and if one is added it needs approval."""
    external = [t for t in tools.REGISTRY.all() if t.effect is Effect.EXTERNAL]
    assert external == [], f"unexpected external-effect tools: {[t.name for t in external]}"


def test_the_registry_refuses_an_unapproved_external_tool():
    """Enforced at registration, so the process will not start misconfigured."""
    registry = Registry()
    with pytest.raises(ValueError, match="EXTERNAL effect"):
        registry.add(
            Tool(
                name="send_it",
                description="x",
                parameters={"type": "object", "properties": {}},
                handler=lambda: None,
                effect=Effect.EXTERNAL,
                requires_approval=False,
            )
        )


def test_every_write_tool_is_reversible_and_local():
    """The two write tools are the whole surface that changes state. Keeping the list short
    is what makes it reviewable, so this pins the count."""
    writes = sorted(t.name for t in tools.REGISTRY.all() if t.effect is Effect.WRITE)
    assert writes == ["record_finding", "shortlist_job"]


# ─── Describing tools to a model ────────────────────────────────────────────


def test_every_tool_is_described_well_enough_to_be_chosen_correctly():
    """A vague description produces a confident wrong tool call, which is worse than no
    tool. These thresholds are crude but they catch a one-line placeholder."""
    for tool in tools.REGISTRY.all():
        assert len(tool.description.strip()) > 80, f"{tool.name} needs a real description"
        assert tool.safety_note, f"{tool.name} does not say why it is safe to expose"
        for key, schema in (tool.parameters.get("properties") or {}).items():
            assert schema.get("description"), f"{tool.name}.{key} has no description"


def test_specs_are_shaped_for_a_model():
    for spec in tools.REGISTRY.specs():
        assert spec["type"] == "function"
        assert spec["function"]["name"]
        assert spec["function"]["parameters"]["type"] == "object"


# ─── Toolsets are permission boundaries ─────────────────────────────────────


def test_every_toolset_names_real_tools():
    for name, wanted in tools.TOOLSETS.items():
        missing = [t for t in wanted if tools.REGISTRY.get(t) is None]
        assert missing == [], f"toolset {name!r} refers to tools that do not exist: {missing}"


def test_an_unknown_toolset_grants_nothing():
    """The safe direction. An agent with no tools falls back to its deterministic path,
    which works; an agent accidentally handed everything does not."""
    assert tools.toolset("no_such_agent") == []


def test_the_ranking_agent_cannot_change_anything():
    """Ranking runs over hundreds of jobs. Side effects there would be unbounded."""
    for tool in tools.toolset("opportunity"):
        assert tool.effect is Effect.READ, f"{tool.name} can write during a ranking pass"


def test_the_resume_agent_cannot_look_up_a_company():
    """Unevidenced company detail on a resume comes from exactly this door being open."""
    names = {t.name for t in tools.toolset("resume")}
    assert "read_company_research" not in names
    assert "find_openings_at_company" not in names


def test_the_writing_agent_can_only_learn_about_a_company_from_stored_research():
    names = {t.name for t in tools.toolset("writing")}
    assert "read_company_research" in names
    assert not {"find_openings_at_company", "check_posting_live"} & names


def test_the_learning_agent_cannot_act_on_its_own_conclusions():
    """It may record a finding. It may not shortlist, prepare or apply."""
    names = {t.name for t in tools.toolset("learning")}
    assert "record_finding" in names
    assert "shortlist_job" not in names


def test_the_supervisor_decides_but_does_not_do():
    names = {t.name for t in tools.toolset("supervisor")}
    assert "shortlist_job" in names, "the supervisor must be able to queue work"
    # It routes; the specialists produce documents and touch forms.
    assert not {"check_claims_supported"} & names


# ─── The boundary itself ────────────────────────────────────────────────────


async def test_an_unknown_tool_fails_with_the_available_names():
    """A model that guessed a name should be able to correct itself from the error."""
    result = await tools.invoke("invent_a_job", {})
    assert not result.ok
    assert result.error is ToolError.NOT_FOUND
    assert "read_candidate_facts" in result.detail


async def test_a_missing_required_argument_is_reported_not_raised():
    result = await tools.invoke("score_job_fit", {})
    assert not result.ok
    assert result.error is ToolError.BAD_ARGUMENTS
    assert "job_id" in result.detail


async def test_an_unexpected_argument_is_refused():
    """Silently dropping it would let a model believe a filter was applied."""
    result = await tools.invoke("resolve_job_family", {"title": "x", "sneaky": "y"})
    assert not result.ok
    assert "sneaky" in result.detail


async def test_a_wrong_type_is_refused():
    result = await tools.invoke("read_candidate_facts", {"limit": "forty"})
    assert not result.ok
    assert "integer" in result.detail


async def test_a_boolean_does_not_pass_as_an_integer():
    """`bool` is a subclass of `int`, so an unguarded isinstance check would let True
    through and it would become 1 somewhere downstream."""
    result = await tools.invoke("read_candidate_facts", {"limit": True})
    assert not result.ok
    assert result.error is ToolError.BAD_ARGUMENTS


async def test_a_value_outside_the_enum_is_refused():
    result = await tools.invoke("read_candidate_facts", {"kind": "salary"})
    assert not result.ok
    assert "must be one of" in result.detail


async def test_a_handler_failure_becomes_a_result_not_an_exception():
    """An exception inside an agent loop either kills the run or becomes "something went
    wrong". A result the agent can read lets it try something else."""
    registry_backup = tools.REGISTRY._tools.copy()
    try:

        @tools.tool(
            "explode_for_test",
            "A tool that raises, to prove the loop survives it. " + "x" * 60,
            safety_note="test only",
        )
        async def explode() -> None:
            raise RuntimeError("kaboom")

        result = await tools.invoke("explode_for_test", {})
        assert not result.ok
        assert result.error is ToolError.FAILED
        assert "kaboom" in result.detail
    finally:
        tools.REGISTRY._tools = registry_backup


async def test_a_handler_can_fail_with_a_specific_reason():
    """`ToolFailure` exists because returning `{"error": ...}` with `ok: true` means an
    agent reads a failure as data."""
    registry_backup = tools.REGISTRY._tools.copy()
    try:

        @tools.tool(
            "missing_thing_for_test",
            "A tool that reports a specific not-found failure. " + "x" * 60,
            safety_note="test only",
        )
        async def missing() -> None:
            raise ToolFailure(ToolError.NOT_FOUND, "no such widget")

        result = await tools.invoke("missing_thing_for_test", {})
        assert not result.ok
        assert result.error is ToolError.NOT_FOUND
        assert result.detail == "no such widget"
    finally:
        tools.REGISTRY._tools = registry_backup


async def test_a_slow_tool_times_out_rather_than_hanging_a_run():
    import asyncio

    registry_backup = tools.REGISTRY._tools.copy()
    try:

        @tools.tool(
            "sleep_forever_for_test",
            "A tool that never answers, to prove the timeout works. " + "x" * 60,
            timeout_s=0.05,
            safety_note="test only",
        )
        async def forever() -> None:
            await asyncio.sleep(30)

        result = await tools.invoke("sleep_forever_for_test", {})
        assert not result.ok
        assert result.error is ToolError.TIMEOUT
    finally:
        tools.REGISTRY._tools = registry_backup


async def test_an_approval_gated_tool_is_refused_without_approval():
    """Approval is a parameter threaded from a human click, not ambient state — nothing a
    model emits can set it."""
    registry_backup = tools.REGISTRY._tools.copy()
    try:

        @tools.tool(
            "needs_a_human_for_test",
            "A tool standing in for something irreversible. " + "x" * 60,
            effect=Effect.EXTERNAL,
            requires_approval=True,
            safety_note="test only",
        )
        async def gated() -> str:
            return "done"

        refused = await tools.invoke("needs_a_human_for_test", {})
        assert not refused.ok
        assert refused.error is ToolError.REFUSED

        allowed = await tools.invoke("needs_a_human_for_test", {}, approved=True)
        assert allowed.ok
        assert allowed.data == "done"
    finally:
        tools.REGISTRY._tools = registry_backup


def test_a_duplicate_tool_name_is_refused():
    """Silently replacing a tool would change what an agent can do with no diff at the
    call site."""
    with pytest.raises(ValueError, match="already registered"):
        tools.REGISTRY.add(
            Tool(
                name="score_job_fit",
                description="x",
                parameters={"type": "object", "properties": {}},
                handler=lambda: None,
            )
        )


# ─── What the model reads back ──────────────────────────────────────────────


def test_a_large_result_is_truncated():
    """A tool returning a whole job board would push the candidate's own facts out of the
    context window — and those are the thing the model must not lose sight of."""
    from careeragent.tools.base import MAX_RESULT_CHARS, ToolResult

    huge = ToolResult.success("x", {"blob": "y" * (MAX_RESULT_CHARS * 2)})
    rendered = huge.for_model()
    assert "truncated" in rendered
    assert len(rendered) < MAX_RESULT_CHARS + 200


def test_a_failure_is_legible_to_the_model():
    from careeragent.tools.base import ToolResult

    rendered = ToolResult.failure("x", ToolError.TIMEOUT, "no answer within 20s").for_model()
    assert '"ok": false' in rendered
    assert "timeout" in rendered


def test_an_unserialisable_result_still_renders():
    """A handler returning an object json cannot encode must not break the agent loop."""
    from careeragent.tools.base import ToolResult

    class Opaque:
        pass

    assert '"ok": true' in ToolResult.success("x", {"o": Opaque()}).for_model()


# ─── The tool log is what makes an agent auditable ──────────────────────────


def test_the_tool_log_records_what_an_agent_actually_did():
    """An agent's reasoning is not auditable. Its tool calls are."""
    from careeragent.tools.base import ToolResult

    log = ToolLog()
    log.record("score_job_fit", {"job_id": "j1"}, ToolResult.success("score_job_fit", {}))
    log.record("score_job_fit", {"job_id": "j2"}, ToolResult.success("score_job_fit", {}))
    log.record(
        "check_posting_live",
        {"url": "https://example.com"},
        ToolResult.failure("check_posting_live", ToolError.TIMEOUT, "slow"),
    )

    assert len(log) == 3
    # Unique, in call order — this is what goes into `agent_decision.tools_used`.
    assert log.names() == ["score_job_fit", "check_posting_live"]
    assert [f["tool"] for f in log.failures()] == ["check_posting_live"]


def test_the_tool_log_summarises_arguments_rather_than_copying_them():
    """An agent can pass a whole job description. The trail should say which tool was
    used, not duplicate its input."""
    from careeragent.tools.base import ToolResult

    log = ToolLog()
    log.record("check_claims_supported", {"text": "z" * 5000}, ToolResult.success("x", {}))
    assert len(log.calls[0]["arguments"]["text"]) <= 120


# ─── Exposed honestly ───────────────────────────────────────────────────────


def test_the_layer_is_readable_over_http():
    from careeragent.api import app

    with TestClient(app) as client:
        body = client.get("/api/v1/decisions/tools").json()
        assert body["count"] == len(tools.REGISTRY.all())
        # The interesting assertion: this system cannot reach the outside world on its own.
        assert body["by_effect"]["external"] == []
        assert "submit_application" in body["absent_by_design"]
        assert set(body["toolsets"]) == set(tools.TOOLSETS)
