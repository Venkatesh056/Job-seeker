"""Jobs and the company rows they name.

One bug, found by asking a question the graph made askable and getting an empty answer that
looked exactly like a true one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.usefixtures("isolated_db")

# ─── Jobs and the company rows they name ────────────────────────────────────
#
# Measured on the live corpus: `job.company_id` was NULL on all 1,748 rows. The column and
# the foreign key had existed since the first migration; the only caller that filled them
# was the per-company harvest, and the broad searches — which produced almost the whole
# corpus — do not know a company id.
#
# It stayed invisible until people arrived, because people hang off `company.id`. "Who
# works at a company I have an opening with" then returned nothing, which is also what an
# empty database returns.


async def _company_and_job(name: str, job_company: str) -> tuple[str, str]:
    from careeragent.models.job import RawJob
    from careeragent.pipeline.normalize import normalize_all
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo

    await init_db(drop=True)
    async with session_scope() as session:
        company = await repo.upsert_company(session, name, domain=f"{name.lower()}.example")
        company_id = company.id

    jobs = normalize_all(
        [
            RawJob(
                source="greenhouse",
                source_job_id="1",
                title="ML Engineer",
                url="https://boards.greenhouse.io/x/jobs/1",
                company_name=job_company,
                location="Bangalore, India",
                description="Python and PyTorch. " * 5,
            )
        ]
    )
    async with session_scope() as session:
        await repo.upsert_jobs(session, jobs)
    return company_id, jobs[0].job_id


async def test_a_harvested_job_is_attached_to_the_company_row_that_already_exists():
    """The forward fix. No caller passes a company id on a broad search, so the link has to
    be resolved from the name the posting carries."""
    from careeragent.storage import session_scope
    from careeragent.storage.models import Job

    company_id, job_id = await _company_and_job("Karya", "Karya")
    async with session_scope() as session:
        row = await session.get(Job, job_id)
        assert row is not None
        assert row.company_id == company_id


async def test_a_job_naming_a_company_we_have_never_heard_of_stays_unlinked():
    """Nothing is created to satisfy the join.

    `company` means "a company we know something about" — it is what the dashboard counts
    and what people, research and outreach hang off. Inserting a bare row for every name in
    a search result would quietly turn 66 researched companies into several hundred
    strings, and the number on the screen would stop meaning what it says.
    """
    from careeragent.storage import session_scope
    from careeragent.storage.models import Company, Job

    _company_id, job_id = await _company_and_job("Karya", "Some Other Co")
    async with session_scope() as session:
        row = await session.get(Job, job_id)
        assert row is not None
        assert row.company_id is None
        count = len((await session.execute(select(Company))).scalars().all())
        assert count == 1, "a company row was invented to satisfy the join"


async def test_the_backfill_links_jobs_harvested_before_the_company_was_researched():
    """The ordinary case, not a one-off migration: a company researched today has its jobs
    already in the table from last week, unlinked."""
    from careeragent.models.job import RawJob
    from careeragent.pipeline.normalize import normalize_all
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo
    from careeragent.storage.models import Job

    await init_db(drop=True)
    jobs = normalize_all(
        [
            RawJob(
                source="greenhouse",
                source_job_id="1",
                title="ML Engineer",
                url="https://boards.greenhouse.io/x/jobs/1",
                company_name="Karya",
                location="Bangalore, India",
                description="Python. " * 5,
            )
        ]
    )
    async with session_scope() as session:
        await repo.upsert_jobs(session, jobs)
        row = await session.get(Job, jobs[0].job_id)
        assert row is not None and row.company_id is None, "no company existed yet"

    async with session_scope() as session:
        company = await repo.upsert_company(session, "Karya")
        company_id = company.id

    # Dry run changes nothing and says what it would do.
    async with session_scope() as session:
        report = await repo.link_jobs_to_companies(session, dry_run=True)
        assert report == {"unlinked": 1, "linked": 1, "still_unlinked": 0}
    async with session_scope() as session:
        row = await session.get(Job, jobs[0].job_id)
        assert row is not None and row.company_id is None, "a dry run wrote to the database"

    async with session_scope() as session:
        await repo.link_jobs_to_companies(session, dry_run=False)
    async with session_scope() as session:
        row = await session.get(Job, jobs[0].job_id)
        assert row is not None and row.company_id == company_id


async def test_the_backfill_will_not_match_a_similar_name():
    """Normalized equality and nothing looser.

    A fuzzy match would attach one company's people, research and outreach history to
    another with a similar name, and every answer built on that join would then be
    confidently wrong rather than visibly empty.
    """
    from careeragent.models.job import RawJob
    from careeragent.pipeline.normalize import normalize_all
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo

    await init_db(drop=True)
    jobs = normalize_all(
        [
            RawJob(
                source="greenhouse",
                source_job_id="1",
                title="ML Engineer",
                url="https://boards.greenhouse.io/x/jobs/1",
                company_name="Karya Labs",
                location="Bangalore, India",
                description="Python. " * 5,
            )
        ]
    )
    async with session_scope() as session:
        await repo.upsert_jobs(session, jobs)
        await repo.upsert_company(session, "Karya")

    async with session_scope() as session:
        report = await repo.link_jobs_to_companies(session, dry_run=False)
    assert report["linked"] == 0
    assert report["still_unlinked"] == 1
