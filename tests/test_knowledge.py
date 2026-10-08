"""The knowledge graph — addendum §14–19.

Two things are worth testing about a projection, and neither is "does it produce a picture".

**It must agree with the rest of the system.** A graph that draws an edge from a job to a
skill the scorer did not count is a second opinion nobody asked for, and the disagreement is
invisible because both look plausible. So the skill matching goes through one function, and
that function is tested hard — it is where a real bug lived.

**It must be honest about being partial.** Every view here is capped, and a capped graph that
does not say so invites the reader to conclude they have seen everything.

The matching tests come first because the bug they pin was severe: a job description
mentioning none of a candidate's skills scored full marks on skill overlap, because
"excellent" contains "excel", "digital" contains "git" and "r" sits inside most English
words. Every job in the database had been scored that way.
"""

from __future__ import annotations

import pytest

from careeragent import knowledge
from careeragent.knowledge import questions
from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind, SearchConfig
from careeragent.models.job import RawJob
from careeragent.pipeline.normalize import normalize_all
from careeragent.pipeline.score import ScoreContext, _skill_overlap, score_job
from careeragent.resume.taxonomy import mentioned_skills, mentions

# ─── The matcher ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "skill"),
    [
        ("We need excellent communication skills.", "excel"),
        ("A digital-first mindset.", "git"),
        ("Strong organisational rigour.", "go"),
        ("Category management experience.", "c"),
        ("Position available immediately.", "pos"),
        ("Working with stakeholders.", "r"),
        ("Experience in marketing.", "r"),
        ("mssql server administration", "sql"),
    ],
    ids=lambda v: str(v)[:28],
)
def test_a_skill_is_not_matched_inside_another_word(text, skill):
    """The bug, by example. Each of these used to count as a matched skill."""
    assert not mentions(text, skill)


@pytest.mark.parametrize(
    ("text", "skill"),
    [
        ("Experience with Go and Rust.", "go"),
        ("Advanced Excel modelling.", "excel"),
        ("Comfortable with Git and CI.", "git"),
        ("Strong R and SAS skills.", "r"),
        ("We write C++ daily.", "c++"),
        ("Built on node.js.", "node.js"),
        ("Python 3 and SQL.", "python"),
        ("Python 3 and SQL.", "sql"),
        ("Deep .NET experience.", ".net"),
        ("Using CI/CD pipelines.", "ci/cd"),
    ],
    ids=lambda v: str(v)[:28],
)
def test_a_skill_is_still_matched_when_it_is_really_there(text, skill):
    """The fix must not trade false positives for false negatives. Skill names ending in
    symbols — c++, .net, node.js — are exactly what a naive `\\b` gets wrong."""
    assert mentions(text, skill)


def test_matching_is_case_insensitive():
    assert mentions("We use PYTHON and Sql.", "python")
    assert mentions("We use PYTHON and Sql.", "sql")


def test_empty_input_matches_nothing():
    assert not mentions("", "python")
    assert not mentions("python", "")


def test_mentioned_skills_preserves_the_order_it_was_given():
    found = mentioned_skills("We use Python, SQL and Docker.", ["docker", "python", "java", "sql"])
    assert found == ["docker", "python", "sql"]


# ─── The scorer is the thing that was wrong ─────────────────────────────────


def _skill(label: str, *aliases: str) -> Fact:
    return Fact(
        kind=FactKind.SKILL,
        label=label,
        aliases=list(aliases),
        evidence_source=EvidenceSource.MASTER_RESUME,
        evidence_locator="cv.pdf#page=1",
    )


def test_a_job_mentioning_none_of_my_skills_scores_none_of_them():
    """The measured failure: this description matched excel, git, go and r, and scored 1.0
    on skill overlap — full marks for a job with no overlap at all."""
    candidate = Candidate(
        name="T",
        facts=[_skill("excel"), _skill("git"), _skill("r"), _skill("go"), _skill("python")],
    )
    job = normalize_all(
        [
            RawJob(
                source="greenhouse",
                source_job_id="1",
                title="Communications Lead",
                url="https://x/1",
                company_name="Acme",
                description=(
                    "We want excellent communication, a digital-first mindset, strong "
                    "organisational rigour and a good grasp of category management."
                ),
            )
        ]
    )[0]

    score, matched, _missing = _skill_overlap(candidate, job)
    assert matched == []
    assert score == 0.0


def test_a_job_that_does_mention_my_skills_still_scores_them():
    candidate = Candidate(name="T", facts=[_skill("python"), _skill("sql")])
    job = normalize_all(
        [
            RawJob(
                source="greenhouse",
                source_job_id="2",
                title="Data Analyst",
                url="https://x/2",
                company_name="Acme",
                description="Daily work in Python against a SQL warehouse. " * 6,
            )
        ]
    )[0]

    _score, matched, _missing = _skill_overlap(candidate, job)
    assert set(matched) == {"python", "sql"}


def test_the_end_to_end_score_reflects_the_fix():
    candidate = Candidate(name="T", facts=[_skill("excel"), _skill("git")])
    job = normalize_all(
        [
            RawJob(
                source="greenhouse",
                source_job_id="3",
                title="Programme Manager",
                url="https://x/3",
                company_name="Acme",
                description="Excellent stakeholder skills and a digital mindset. " * 6,
            )
        ]
    )[0]
    score = score_job(job, ScoreContext(candidate=candidate, config=SearchConfig(roles=[])))
    assert score.skills == 0.0
    assert score.matched_skills == []


# ─── Node identity ──────────────────────────────────────────────────────────


def test_an_alias_and_its_canonical_name_are_one_node():
    """The classic graph bug: "TF" from a job and "tensorflow" from a resume become two
    circles, the candidate looks unconnected, and the picture says the opposite of the
    truth."""
    assert knowledge.skill_node("tf").id == knowledge.skill_node("tensorflow").id


def test_node_ids_are_derived_so_the_same_thing_found_twice_is_one_node():
    assert knowledge.company_node("Swiggy").id == knowledge.company_node("swiggy ").id
    assert knowledge.skill_node("Power BI").id == knowledge.skill_node("power bi").id


def test_a_node_id_carries_its_kind():
    node = knowledge.skill_node("python")
    assert node.id == "skill:python"
    assert node.id.split(":", 1)[0] == knowledge.NodeKind.SKILL.value


def test_one_city_is_one_node():
    from careeragent.knowledge.model import location_node

    assert location_node("Bengaluru, KA").id == location_node("Bangalore, India").id


# ─── The graph container ────────────────────────────────────────────────────


def test_a_node_added_twice_appears_once():
    graph = knowledge.Graph()
    graph.add(knowledge.skill_node("python"))
    graph.add(knowledge.skill_node("python"))
    assert len(graph) == 1


def test_adding_a_node_again_merges_what_the_second_pass_knew():
    """A job reached first as a neighbour and later as the subject should keep both."""
    graph = knowledge.Graph()
    graph.add(knowledge.Node(knowledge.NodeKind.JOB, "j1", "Analyst", {"score": 0.8}))
    graph.add(knowledge.Node(knowledge.NodeKind.JOB, "j1", "Analyst", {"root": True}))
    node = graph.get("job:j1")
    assert node is not None
    assert node.attrs == {"score": 0.8, "root": True}


def test_an_edge_to_a_missing_node_is_dropped():
    """Otherwise it draws as a line into empty space once the cap removes one end."""
    graph = knowledge.Graph()
    graph.add(knowledge.skill_node("python"))
    graph.link(knowledge.Edge(knowledge.EdgeKind.REQUIRES_SKILL, "job:nope", "skill:python"))
    assert graph.edges == []


def test_a_capped_graph_says_it_is_capped():
    """A viewer who concludes "that is all of it" from a truncated picture has drawn the
    wrong conclusion from a true one."""
    graph = knowledge.Graph(max_nodes=2)
    for i in range(6):
        graph.add(knowledge.Node(knowledge.NodeKind.JOB, f"j{i}", f"Job {i}"))
    assert len(graph) == 2
    assert graph.truncated is True
    assert graph.to_dict()["truncated"] is True


def test_an_uncapped_graph_does_not_claim_to_be():
    graph = knowledge.Graph(max_nodes=50)
    graph.add(knowledge.skill_node("python"))
    assert graph.to_dict()["truncated"] is False


def test_the_graph_counts_what_it_holds():
    graph = knowledge.Graph()
    graph.add(knowledge.skill_node("python"))
    graph.add(knowledge.skill_node("sql"))
    graph.add(knowledge.company_node("Acme"))
    assert graph.counts() == {"skill": 2, "company": 1}


# ─── The questions ──────────────────────────────────────────────────────────


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'g.db'}")
    monkeypatch.setenv("CAREERAGENT_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("CAREERAGENT_CACHE_DIR", str(tmp_path / "cache"))

    from careeragent import config as config_module
    from careeragent import storage

    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]
    yield tmp_path
    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]


async def _seed(skills: list[str], descriptions: list[str]) -> str:
    """A candidate with `skills`, and one job per description, all scored."""
    from careeragent.models.scoring import JobScore as ScoreModel
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo

    await init_db(drop=True)
    model = Candidate(name="T", email="t@example.com", facts=[_skill(s) for s in skills])
    jobs = normalize_all(
        [
            RawJob(
                source="greenhouse",
                source_job_id=str(i),
                title=f"Role {i}",
                url=f"https://boards.greenhouse.io/acme/jobs/{i}",
                company_name=f"Company{i}",
                location="Bangalore, India",
                description=text,
            )
            for i, text in enumerate(descriptions)
        ]
    )
    async with session_scope() as session:
        row = await repo.save_candidate_profile(session, model)
        await repo.replace_facts(session, row.id, model.facts)
        await repo.upsert_jobs(session, jobs)
        for job in jobs:
            await repo.save_score(
                session, row.id, ScoreModel(job_id=job.job_id, final_score=0.7, job_fit=0.7)
            )
        return row.id


async def test_an_ambiguous_name_is_never_recommended_as_a_skill_to_learn(isolated):
    """ "Go", "C" and "Excel" are ordinary English. Even with word boundaries, "we want you
    to go beyond" reads as a Go requirement, and telling a data scientist to learn C on that
    basis is worse than saying nothing.

    Asserted on the output, not on the code: a test that checked the import would pass with
    the value imported and unused.
    """
    from careeragent.storage import session_scope

    candidate_id = await _seed(
        ["python"],
        [
            "We want you to go beyond the brief and excel at category management. " * 4,
            "Kubernetes and AWS experience required, working in Python. " * 4,
        ],
    )
    async with session_scope() as session:
        rows = await questions.bridging_skills(session, candidate_id, limit=20, min_score=0.0)

    names = {row["skill"] for row in rows}
    assert not names & {"go", "excel", "c", "git"}, f"recommended an English word: {names}"
    # And the unambiguous ones in the same corpus still come through.
    assert {"kubernetes", "aws"} & names


async def test_an_ambiguous_skill_the_candidate_has_is_reported_with_a_warning(isolated):
    """Different from a recommendation: this is the candidate's own skill list, and a number
    they cannot see the reason for is worse than one labelled unreliable."""
    from careeragent.storage import session_scope

    candidate_id = await _seed(
        ["excel", "python"],
        ["Advanced Excel modelling and Python scripting. " * 4],
    )
    async with session_scope() as session:
        rows = await questions.skill_reach(session, candidate_id)

    by_skill = {row["skill"]: row for row in rows}
    assert by_skill["excel"]["ambiguous"] is True
    assert "runs high" in by_skill["excel"]["note"]
    assert by_skill["python"]["ambiguous"] is False
    assert by_skill["python"]["note"] == ""


async def test_a_skill_nothing_mentions_is_flagged_rather_than_left_blank(isolated):
    """Zero reach usually means the Fact Bank spells it differently from the market, which
    is a finding. A blank cell is not."""
    from careeragent.storage import session_scope

    candidate_id = await _seed(["neo4j", "python"], ["Python scripting all day. " * 5])
    async with session_scope() as session:
        rows = await questions.skill_reach(session, candidate_id)

    neo4j = next(r for r in rows if r["skill"] == "neo4j")
    assert neo4j["reach"] == 0
    assert "check how it is named" in neo4j["note"]


async def test_the_candidate_graph_connects_the_person_to_the_work(isolated):
    """The default view has to actually join up: me → skill → job → company."""
    from careeragent.storage import session_scope

    candidate_id = await _seed(["python", "sql"], ["Daily Python against a SQL warehouse. " * 5])
    async with session_scope() as session:
        graph = await knowledge.candidate_graph(session, candidate_id)

    kinds = graph.counts()
    assert kinds["candidate"] == 1
    assert kinds["skill"] == 2
    assert kinds["job"] >= 1
    assert kinds["company"] >= 1

    me = f"candidate:{candidate_id}"
    assert graph.has(me)
    assert "skill:python" in graph.neighbours(me)
    # And the job is reachable from the skill, which is the whole point of the projection.
    assert any(n.startswith("job:") for n in graph.neighbours("skill:python"))


async def test_an_unknown_candidate_produces_an_empty_graph_not_an_error(isolated):
    from careeragent.storage import init_db, session_scope

    await init_db(drop=True)
    async with session_scope() as session:
        graph = await knowledge.candidate_graph(session, "no-such-candidate")
    assert len(graph) == 0


async def test_the_graph_is_readable_over_http(isolated):
    from fastapi.testclient import TestClient

    from careeragent.api import app

    await _seed(["python"], ["Python work. " * 5])
    with TestClient(app) as client:
        body = client.get("/api/v1/graph").json()
        assert body["node_count"] > 0
        assert "truncated" in body

        stats = client.get("/api/v1/graph/stats").json()
        assert stats["skills"] == 1

        # A malformed node id is refused rather than guessed at.
        assert client.get("/api/v1/graph/around", params={"node": "swiggy"}).status_code == 422
