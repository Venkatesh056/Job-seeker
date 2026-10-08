"""Command-line entry point.

    careeragent status                       what is configured and working
    careeragent init                         create the database
    careeragent ingest-resume my_cv.pdf      resume -> Fact Bank
    careeragent facts                        review the Fact Bank
    careeragent discover stripe.com          find, score and store jobs
    careeragent jobs                         ranked jobs from the database
    careeragent gaps                         skills that keep costing applications
    careeragent tailor <job-id>              job-specific resume, validated
    careeragent apply <job-id>               prepare an application (never submits)
    careeragent queue                        what is waiting on you
    careeragent approve <application-id>     submit one you have reviewed
    careeragent takeover <application-id>    open a visible browser and finish it yourself
    careeragent board                        the application tracker
    careeragent companies --research 10      company intelligence and fit scores
    careeragent outreach                     companies worth contacting
    careeragent analytics                    funnel, rates, cost per application
    careeragent learn                        what the outcomes show
    careeragent rescore                      recompute stored scores in place
    careeragent monitor                      watch company boards for new openings
    careeragent migrate                      bring the schema up to date

Everything here runs with no API key. Discovery uses public ATS feeds and
semantic matching runs on-device.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import json as _json
from pathlib import Path
from typing import Annotated

import typer
from rich.panel import Panel
from rich.table import Table

from .config import get_settings
from .console import console
from .logging import configure_logging
from .models.candidate import Candidate, EvidenceSource, Fact, FactKind, SearchConfig
from .pipeline import embeddings as emb
from .sources.ats import PROVIDERS
from .sources.ats.discover import domain_of
from .sources.seeds import SEGMENTS
from .sources.seeds import segment as seed_segment

app = typer.Typer(add_completion=False, help="AI Career Agent", no_args_is_help=True)


def _setup() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


# ─── Status and setup ───────────────────────────────────────────────────────


@app.command()
def status() -> None:
    """Show what is configured, stored and available."""
    _setup()
    from .storage import resolve_url

    asyncio.run(_status(resolve_url()))


async def _status(url: str) -> None:
    from .storage import repository as repo
    from .storage import session_scope

    table = Table(title="AI Career Agent", header_style="bold", show_header=False)
    table.add_column("", style="bold")
    table.add_column("")

    dialect = "PostgreSQL + pgvector" if url.startswith("postgresql") else "SQLite (local file)"
    table.add_row("Database", dialect)

    try:
        async with session_scope() as session:
            candidate = await repo.load_candidate(session)
            job_total = await repo.job_count(session)
            boards = len(await repo.known_boards(session))
        if candidate:
            table.add_row("Candidate", f"{candidate.name} — {len(candidate.facts)} facts")
            table.add_row("Skills", str(len(candidate.skill_labels())))
        else:
            table.add_row("Candidate", "[yellow]none — run 'careeragent ingest-resume'[/yellow]")
        table.add_row("Jobs stored", str(job_total))
        table.add_row("Known ATS boards", str(boards))
    except Exception as exc:
        table.add_row("Database", f"[red]unreachable: {str(exc)[:60]}[/red]")
        table.add_row("Fix", "run 'careeragent init'")

    table.add_row(
        "Semantic matching",
        "[green]on-device[/green]" if emb.available() else "[yellow]model unavailable[/yellow]",
    )
    table.add_row("ATS feeds", f"{len(PROVIDERS)} platforms")

    settings = get_settings()
    table.add_row("Autonomy level", f"{settings.autonomy_level} (never auto-submits below 5)")
    table.add_row(
        "Portal adapters",
        "enabled" if settings.enable_portal_adapters else "[dim]disabled (platform terms)[/dim]",
    )
    console.print(table)


@app.command()
def init(
    reset: Annotated[bool, typer.Option("--reset", help="Drop and recreate all tables")] = False,
) -> None:
    """Create the database schema."""
    _setup()
    from .storage import init_db, resolve_url

    if reset and not typer.confirm(
        "This deletes every stored job, fact and application. Continue?"
    ):
        raise typer.Abort
    # force: the user has just been asked and said yes, which is exactly the case the
    # guard in init_db is there to distinguish from an accident.
    asyncio.run(init_db(drop=reset, force=True))
    console.print(f"[green]Database ready[/green] — {resolve_url()}")


@app.command()
def providers() -> None:
    """List the supported ATS platforms and their feed endpoints."""
    _setup()
    table = Table(title="Supported ATS job-board feeds", header_style="bold")
    table.add_column("Provider")
    table.add_column("Method")
    table.add_column("Empty = exists?")
    table.add_column("Example endpoint", overflow="fold")
    for provider in PROVIDERS:
        table.add_row(
            provider.label,
            provider.method,
            "yes" if provider.empty_means_exists else "no — proves nothing",
            provider.endpoint(provider.example_slug),
        )
    console.print(table)
    console.print(
        "\n[dim]'Empty = exists?' matters: platforms marked 'no' answer 200 with an empty\n"
        "list for a board that never existed, which is indistinguishable from a company\n"
        "with no openings. Those need careers-page discovery to confirm the slug.[/dim]"
    )


# ─── Resume ─────────────────────────────────────────────────────────────────


@app.command(name="ingest-resume")
def ingest_resume(
    path: Annotated[Path, typer.Argument(help="Your master resume (.pdf, .docx, .txt, .md)")],
    show: Annotated[bool, typer.Option("--show", help="Print every extracted fact")] = False,
    save: Annotated[bool, typer.Option("--save/--dry-run", help="Write to the Fact Bank")] = True,
) -> None:
    """Parse a resume into the Fact Bank.

    Nothing is invented: every fact records the line it came from, and all facts
    land unverified until you confirm them.
    """
    _setup()
    from .resume import parse_resume, to_candidate

    try:
        parsed = parse_resume(path)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    if not parsed.facts:
        console.print("[red]No facts could be extracted.[/red]")
        for warning in parsed.warnings:
            console.print(f"  [yellow]{warning}[/yellow]")
        raise typer.Exit(code=1)

    console.print(
        Panel(
            "\n".join(f"[bold]{k.title():10}[/bold] {v}" for k, v in parsed.contact.items())
            or "[yellow]no contact details found[/yellow]",
            title="Contact",
            expand=False,
        )
    )

    table = Table(title="Fact Bank", header_style="bold")
    table.add_column("Kind")
    table.add_column("Count", justify="right")
    table.add_column("Examples", overflow="fold", style="dim")
    for kind in FactKind:
        found = parsed.facts_of(kind)
        if found:
            table.add_row(kind.value, str(len(found)), ", ".join(f.label[:32] for f in found[:5]))
    console.print(table)

    for warning in parsed.warnings:
        console.print(f"[yellow]![/yellow] {warning}")

    if show:
        detail = Table(header_style="bold", title="All facts")
        detail.add_column("Kind")
        detail.add_column("Label", overflow="fold")
        detail.add_column("Evidence", style="dim", overflow="fold")
        for fact in parsed.facts:
            detail.add_row(fact.kind.value, fact.label[:70], fact.evidence_locator)
        console.print(detail)

    if not save:
        console.print("\n[dim]Dry run — nothing written.[/dim]")
        return

    asyncio.run(_save_resume(parsed, to_candidate(parsed)))


async def _save_resume(parsed, candidate: Candidate) -> None:
    from .storage import init_db, session_scope
    from .storage import repository as repo
    from .storage.models import ResumeMaster

    await init_db()
    async with session_scope() as session:
        row = await repo.save_candidate_profile(session, candidate)
        added, existing = await repo.replace_facts(session, row.id, parsed.facts)
        session.add(
            ResumeMaster(
                candidate_id=row.id,
                filename=parsed.filename,
                storage_key=parsed.filename,
                content_hash=parsed.content_hash,
                raw_text=parsed.raw_text,
                parsed={"sections": sorted(parsed.sections), "contact": parsed.contact},
            )
        )

    console.print(
        f"\n[green]Saved[/green] {added} new facts"
        + (f" ({existing} already known)" if existing else "")
    )
    console.print(
        "[dim]All facts are unverified. Review them with 'careeragent facts' — "
        "unverified facts are used for matching but never for generated resume claims.[/dim]"
    )


@app.command()
def facts(
    kind: Annotated[str, typer.Option(help="Filter: skill, project, experience, …")] = "",
) -> None:
    """Show the Fact Bank."""
    _setup()
    asyncio.run(_show_facts(kind))


async def _show_facts(kind_filter: str) -> None:
    from .storage import repository as repo
    from .storage import session_scope

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)

    if candidate is None or not candidate.facts:
        console.print(
            "[yellow]Fact Bank is empty.[/yellow] Run 'careeragent ingest-resume <file>'."
        )
        return

    table = Table(title=f"Fact Bank — {candidate.name}", header_style="bold")
    table.add_column("Kind")
    table.add_column("Label", overflow="fold")
    table.add_column("Dates")
    table.add_column("Verified")
    table.add_column("Evidence", style="dim", overflow="fold")

    for fact in candidate.facts:
        if kind_filter and fact.kind.value != kind_filter:
            continue
        dates = ""
        if fact.first_used_at:
            dates = f"{fact.first_used_at:%Y-%m}" + (
                f" -> {fact.last_used_at:%Y-%m}" if fact.last_used_at else " -> now"
            )
        table.add_row(
            fact.kind.value,
            fact.label[:60],
            dates,
            "[green]yes[/green]" if fact.verified else "[yellow]no[/yellow]",
            fact.evidence_locator,
        )
    console.print(table)

    years = candidate.total_years_experience()
    if years is not None:
        console.print(f"\n[bold]Total experience:[/bold] {years} years (from dated entries only)")


# ─── Discovery ──────────────────────────────────────────────────────────────


@app.command()
def discover(
    targets: Annotated[
        list[str] | None, typer.Argument(help="Company domains or board URLs")
    ] = None,
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="File of targets, one per line")
    ] = None,
    roles: Annotated[str, typer.Option(help="Comma-separated target roles")] = "",
    locations: Annotated[str, typer.Option(help="Comma-separated preferred locations")] = "",
    skills: Annotated[str, typer.Option(help="Extra skills, if no resume is ingested")] = "",
    experience: Annotated[str, typer.Option(help="internship | entry | mid | senior")] = "",
    max_age: Annotated[int, typer.Option(help="Only jobs posted within N days")] = 30,
    limit: Annotated[int, typer.Option(help="Rows to display")] = 20,
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Write full results to JSON")
    ] = None,
    no_semantic: Annotated[
        bool, typer.Option("--no-semantic", help="Skip on-device embeddings")
    ] = False,
    llm: Annotated[
        bool, typer.Option("--llm", help="Stage 3: LLM reasoning on jobs past the gate")
    ] = False,
    no_descriptions: Annotated[
        bool, typer.Option("--no-descriptions", help="Faster; skips job text")
    ] = False,
    no_store: Annotated[
        bool, typer.Option("--no-store", help="Do not write to the database")
    ] = False,
    known: Annotated[
        bool, typer.Option("--known", help="Also search every board already discovered")
    ] = False,
    segment: Annotated[
        str, typer.Option(help=f"Curated target list: {', '.join(sorted(SEGMENTS))}")
    ] = "",
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Discover, normalize, deduplicate, score and store jobs."""
    settings = get_settings()
    configure_logging("DEBUG" if verbose else settings.log_level, settings.log_json)

    all_targets = list(targets or [])
    if segment:
        try:
            all_targets += list(seed_segment(segment))
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from exc
    if file:
        all_targets += [
            line.strip()
            for line in file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]

    config = SearchConfig(
        roles=_split(roles),
        locations=_split(locations),
        experience_band=experience or None,
        max_job_age_days=max_age,
    )
    asyncio.run(
        _run_discovery(
            all_targets,
            config,
            _split(skills),
            limit,
            json_out,
            include_descriptions=not no_descriptions,
            use_semantic=not no_semantic,
            use_llm=llm,
            store=not no_store,
            include_known=known,
        )
    )


async def _load_or_build_candidate(extra_skills: list[str]) -> Candidate:
    """Prefer the ingested Fact Bank; fall back to --skills so the tool is
    usable before a resume has been added."""
    from .storage import repository as repo
    from .storage import session_scope

    candidate: Candidate | None = None
    try:
        async with session_scope() as session:
            candidate = await repo.load_candidate(session)
    except Exception:
        candidate = None

    if candidate is None:
        candidate = Candidate(name="(cli)")

    for skill in extra_skills:
        if candidate.supports(skill):
            continue
        candidate.facts.append(
            Fact(
                kind=FactKind.SKILL,
                label=skill,
                evidence_source=EvidenceSource.USER_ENTERED,
                evidence_locator="cli:--skills",
            )
        )
    return candidate


async def _run_discovery(
    targets: list[str],
    config: SearchConfig,
    extra_skills: list[str],
    limit: int,
    json_out: Path | None,
    *,
    include_descriptions: bool,
    use_semantic: bool,
    use_llm: bool,
    store: bool,
    include_known: bool,
    research_companies: bool = True,
) -> None:
    """Run the shared pipeline and render it for a terminal.

    Everything the agent *decides* lives in `pipeline.run.run_discovery` and is
    called from here, not reimplemented. The CLI and the dashboard must reach the
    same conclusion about the same job — and the only way to guarantee that is for
    there to be one implementation.
    """
    from .pipeline.run import run_discovery
    from .storage import init_db, session_scope
    from .storage import repository as repo

    if store:
        await init_db()

    if include_known:
        async with session_scope() as session:
            targets += [f"{b.provider}:{b.slug}" for b in await repo.known_boards(session)]
    targets = list(dict.fromkeys(t for t in targets if t))

    if not targets:
        console.print("[red]No targets.[/red] Pass domains, --file, or --known.")
        raise typer.Exit(code=2)

    candidate = await _load_or_build_candidate(extra_skills)
    if not candidate.skill_labels():
        console.print(
            "[yellow]No skills known.[/yellow] Run 'careeragent ingest-resume <file>' "
            "or pass --skills; scoring will be weak without them.\n"
        )

    weights = None
    if store:
        async with session_scope() as session:
            from .learning import active_weight_model

            row = await repo.get_or_create_candidate(session, candidate.name)
            weights = await active_weight_model(session, row.id)

    console.print(f"[bold]Searching {len(targets)} target(s)...[/bold]")
    with _live_progress():
        result = await run_discovery(
            targets,
            candidate,
            config,
            include_descriptions=include_descriptions,
            use_semantic=use_semantic,
            use_llm=use_llm,
            research_companies=research_companies,
            store=store,
            autonomy_level=get_settings().autonomy_level,
            weights=weights,
        )

    if result.error:
        console.print(f"[red]{result.error}[/red]")
        raise typer.Exit(code=1)

    stats = result.stats
    if not result.jobs:
        console.print(
            "\n[yellow]No jobs found.[/yellow] A 'no_ats_detected' note means the company "
            "uses an ATS we do not read yet, or its careers page is fully JavaScript-rendered."
        )
        return

    console.print(
        f"\n[bold]Funnel[/bold]  discovered {stats.discovered}"
        f" -> unique {stats.unique} (removed {stats.duplicates_removed})"
        f" -> rejected {stats.rejected}"
        f" -> past gate {stats.past_gate}"
        f" -> [green]worth applying {stats.worth_applying}[/green]"
        + (f" -> outreach {stats.outreach_opportunities}" if stats.outreach_opportunities else "")
    )
    if stats.companies_researched:
        console.print(f"[dim]Researched {stats.companies_researched} company website(s).[/dim]")
    if stats.llm_cost_usd:
        console.print(f"[dim]LLM cost this run: ${stats.llm_cost_usd:.5f}[/dim]")

    ranked = result.ranked(limit)
    table = Table(title=f"Top {len(ranked)} opportunities", header_style="bold")
    table.add_column("Score", justify="right")
    table.add_column("Title", overflow="fold")
    table.add_column("Company")
    table.add_column("Location", overflow="fold")
    table.add_column("Co.", justify="right")
    table.add_column("Age", justify="right")
    table.add_column("Decision", overflow="fold", style="dim")

    for job, score in ranked:
        decision = result.decisions.get(job.job_id)
        colour = (
            "green"
            if score.final_score >= 0.75
            else "yellow"
            if score.final_score >= 0.6
            else "red"
        )
        table.add_row(
            f"[{colour}]{score.final_score:.2f}[/{colour}]",
            job.title[:44],
            job.company[:16],
            job.location[:20] or "-",
            f"{score.company_fit:.2f}" if score.company_fit else "-",
            f"{job.age_days}d" if job.age_days is not None else "-",
            f"{decision.action}: {decision.reason}" if decision else "-",
        )
    console.print(table)

    if result.gaps:
        console.print(
            "\n[bold]Skills costing you applications:[/bold] "
            + ", ".join(f"{skill} ({count})" for skill, count in result.gaps[:8])
        )

    if store:
        console.print(f"\n[dim]Stored {stats.unique} jobs. See 'careeragent jobs'.[/dim]")

    if json_out:
        json_out.write_text(
            json.dumps(
                {
                    "run_id": result.run_id,
                    "funnel": stats.as_dict(),
                    "jobs": [
                        {
                            **job.model_dump(mode="json"),
                            "score": score.model_dump(mode="json"),
                            "decision": (
                                result.decisions[job.job_id].__dict__
                                if job.job_id in result.decisions
                                else None
                            ),
                        }
                        for job, score in result.ranked()
                    ],
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        console.print(f"[dim]Full results written to {json_out}[/dim]")


@contextlib.contextmanager
def _live_progress():
    """Mirror the agent's event stream into the terminal while a run is going.

    The same events the dashboard renders, so the two views of a run agree — and so
    a long discovery run shows what it is doing rather than hanging silently.
    """
    from .events import get_bus

    queue: list[str] = []

    class ConsoleSink:
        async def emit(self, event) -> None:
            if event.message:
                queue.append(event.message)
                console.print(f"[dim]  {event.message}[/dim]")

    bus = get_bus()
    sink = ConsoleSink()
    bus.add_sink(sink)
    try:
        yield queue
    finally:
        with contextlib.suppress(ValueError):
            bus._sinks.remove(sink)


# ─── Stored views ───────────────────────────────────────────────────────────


@app.command()
def jobs(
    limit: Annotated[int, typer.Option(help="Rows to show")] = 25,
    min_score: Annotated[float, typer.Option(help="Minimum final score")] = 0.0,
    priority: Annotated[str, typer.Option(help="very_high | high | review | low")] = "",
) -> None:
    """Ranked jobs already stored."""
    _setup()
    asyncio.run(_show_jobs(limit, min_score, priority or None))


async def _show_jobs(limit: int, min_score: float, priority: str | None) -> None:
    from .storage import repository as repo
    from .storage import session_scope

    async with session_scope() as session:
        candidate_row = await repo.get_or_create_candidate(session)
        rows = await repo.top_jobs(session, candidate_row.id, limit, min_score, priority)

    if not rows:
        console.print("[yellow]No scored jobs stored.[/yellow] Run 'careeragent discover'.")
        return

    table = Table(title="Stored opportunities", header_style="bold")
    table.add_column("Score", justify="right")
    table.add_column("Priority")
    table.add_column("Title", overflow="fold")
    table.add_column("Company")
    table.add_column("Location", overflow="fold")
    table.add_column("ATS")
    for job, score in rows:
        table.add_row(
            f"{score.final_score:.2f}",
            score.priority,
            job.title[:46],
            job.company[:18],
            job.location[:22] or "-",
            job.ats or "-",
        )
    console.print(table)


@app.command()
def gaps(limit: Annotated[int, typer.Option()] = 15) -> None:
    """Skills that repeatedly appear in jobs you otherwise fit."""
    _setup()
    asyncio.run(_show_gaps(limit))


async def _show_gaps(limit: int) -> None:
    from sqlalchemy import select

    from .storage import session_scope
    from .storage.models import Gap

    async with session_scope() as session:
        rows = list(
            (await session.execute(select(Gap).order_by(Gap.seen_in_job_count.desc()).limit(limit)))
            .scalars()
            .all()
        )

    if not rows:
        console.print("[yellow]No gaps recorded yet.[/yellow] Run 'careeragent discover'.")
        return

    table = Table(title="Skill gaps", header_style="bold")
    table.add_column("Skill")
    table.add_column("Seen in jobs", justify="right")
    for row in rows:
        table.add_row(row.skill, str(row.seen_in_job_count))
    console.print(table)
    console.print(
        "\n[dim]These are recorded, never added to your resume. "
        "Closing the top one or two is usually the highest-leverage change.[/dim]"
    )


def _company_from(target: str, slug: str) -> str:
    """Best available company name before company research fills it in properly.

    The board slug is the more reliable source: an explicit "greenhouse:stripe"
    target has no domain to read, and parsing one out of it yields the ATS name
    rather than the employer.
    """
    if slug:
        head = slug.split("::")[0]
        if "." in head:  # Workday carries a tenant host
            head = head.split(".")[0]
        return head.replace("-", " ").replace("_", " ").title()
    if domain := domain_of(target):
        return domain.split(".")[0].replace("-", " ").title()
    return target


# ─── Schema ─────────────────────────────────────────────────────────────────


@app.command()
def migrate(
    show: Annotated[
        bool, typer.Option("--show", help="Report state without changing anything")
    ] = False,
) -> None:
    """Bring the database schema up to date.

    Safe to run repeatedly. A database created before migrations existed is adopted
    rather than rebuilt: missing tables and columns are added, nothing is dropped.
    """
    _setup()
    asyncio.run(_migrate(show))


async def _migrate(show: bool) -> None:
    from .storage.migrate import history, inspect_state, upgrade

    state = await inspect_state()
    console.print(
        Panel(
            f"[bold]{state['dialect']}[/bold]\n"
            f"tables: {len(state['tables'])}\n"
            f"alembic managed: {'yes, at ' + str(state.get('version')) if state['alembic_managed'] else 'no'}\n"
            f"missing tables: {', '.join(state['missing_tables']) or 'none'}",
            title="Database",
        )
    )
    if show:
        table = Table(header_style="bold", title="Migrations")
        table.add_column("Revision")
        table.add_column("Message", overflow="fold")
        for revision in history():
            table.add_row(revision["revision"], revision["message"])
        console.print(table)
        return

    result = await upgrade()
    console.print(f"[green]{result['action']}[/green]")
    for key in ("tables_created", "columns_added", "unresolvable"):
        if result.get(key):
            console.print(f"  {key}: {', '.join(result[key])}")
    if result.get("from") or result.get("to"):
        console.print(f"  {result.get('from')} -> {result.get('to')}")


# ─── Resume tailoring ───────────────────────────────────────────────────────


@app.command()
def archetypes() -> None:
    """Resume archetypes and what each one emphasises."""
    from .resume import ARCHETYPES

    table = Table(header_style="bold", title="Resume archetypes")
    table.add_column("id")
    table.add_column("Label")
    table.add_column("Emphasises", overflow="fold", style="dim")
    for archetype in ARCHETYPES:
        table.add_row(
            archetype.id,
            archetype.label,
            ", ".join(archetype.priority_skills[:10]) or "(job-specific; no fixed ordering)",
        )
    console.print(table)
    console.print(
        "\n[dim]An archetype is an ordering over your Fact Bank, not a separate "
        "document — which is why a variant cannot contain anything your master "
        "resume does not.[/dim]"
    )


@app.command()
def tailor(
    job_id: Annotated[str, typer.Argument(help="Job id, from 'careeragent jobs'")],
    archetype: Annotated[
        str, typer.Option(help="Force an archetype; omit to pick from the title")
    ] = "",
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Selection only; no rephrasing")] = False,
    out: Annotated[
        Path | None, typer.Option("--out", help="Also write the plain-text form here")
    ] = None,
) -> None:
    """Tailor your resume for one job, validate it, and render a PDF."""
    _setup()
    asyncio.run(_tailor(job_id, archetype or None, not no_llm, out))


async def _tailor(job_id: str, archetype: str | None, use_llm: bool, out: Path | None) -> None:
    from .applications import service
    from .applications.service import ApplicationError
    from .resume import to_text
    from .resume.documents import TailoredResume
    from .storage import init_db, session_scope
    from .storage import repository as repo
    from .storage.models import Job

    await init_db()
    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            console.print("[red]No candidate.[/red] Run 'careeragent ingest-resume <file>' first.")
            raise typer.Exit(code=2)
        row = await repo.get_or_create_candidate(session)

        job = await session.get(Job, job_id)
        if job is None:
            console.print(
                f"[red]No job with id {job_id}.[/red] Run 'careeragent jobs' to list them."
            )
            raise typer.Exit(code=2)

        console.print(f"[bold]{job.title}[/bold] at {job.company}")
        try:
            application = await service.shortlist(session, job_id, row.id)
            render_id, validation = await service.tailor_for(
                session,
                application,
                candidate,
                archetype=archetype,
                use_llm=use_llm,
                force=True,  # the user typed the command; give them a fresh document
            )
        except ApplicationError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

        render = await repo.get_resume_render(session, render_id) if render_id else None
        if render is None:
            console.print("[red]Nothing was generated.[/red]")
            raise typer.Exit(code=1)

        colour = "green" if validation.get("ok") else "red"
        console.print(
            f"\n[bold]{render.archetype}[/bold]  "
            f"ATS [{colour}]{validation.get('ats_score', 0):.0%}[/{colour}]  "
            f"evidence [{colour}]{validation.get('evidence_integrity', 0):.0%}[/{colour}]  "
            f"keywords {validation.get('keyword_match', 0):.0%}"
        )
        for violation in validation.get("violations") or []:
            console.print(f"  [red]x[/red] {violation['kind']}: {violation['detail']}")
        for warning in validation.get("warnings") or []:
            console.print(f"  [yellow]![/yellow] {warning}")
        if missing := validation.get("missing_keywords"):
            console.print(
                f"\n[dim]Keywords this job wanted that you have no evidence for: "
                f"{', '.join(missing[:10])}[/dim]"
            )
        if render.storage_key:
            console.print(f"\n[green]Written to[/green] {render.storage_key}")

        if out:
            out.write_text(
                to_text(TailoredResume.from_dict(render.content or {})), encoding="utf-8"
            )
            console.print(f"[dim]Plain text written to {out}[/dim]")


# ─── Applications ───────────────────────────────────────────────────────────


@app.command(name="apply")
def apply_command(
    job_id: Annotated[str, typer.Argument(help="Job id, from 'careeragent jobs'")],
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Skip LLM answers and rephrasing")
    ] = False,
    cover_letter: Annotated[
        bool, typer.Option("--cover-letter", help="Also draft a cover letter")
    ] = False,
    show_browser: Annotated[
        bool, typer.Option("--show-browser", help="Run headed so you can watch")
    ] = False,
) -> None:
    """Prepare an application: tailor, open the form, fill what can be proved, stop.

    This never submits. Review it with 'careeragent queue' and submit with
    'careeragent approve <application-id>'.
    """
    _setup()
    asyncio.run(_apply(job_id, not no_llm, cover_letter, not show_browser))


async def _apply(job_id: str, use_llm: bool, cover_letter: bool, headless: bool) -> None:
    from .applications import service
    from .applications.service import ApplicationError
    from .storage import init_db, session_scope
    from .storage import repository as repo

    await init_db()
    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            console.print("[red]No candidate.[/red] Run 'careeragent ingest-resume <file>' first.")
            raise typer.Exit(code=2)
        row = await repo.get_or_create_candidate(session)

        try:
            application = await service.shortlist(session, job_id, row.id)
            with _live_progress():
                result = await service.prepare(
                    session,
                    application.id,
                    candidate,
                    use_llm=use_llm,
                    headless=headless,
                    write_cover_letter=cover_letter,
                )
        except ApplicationError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

    _render_prepare(result)


def _render_prepare(result) -> None:
    status_colour = {
        "awaiting_approval": "green",
        "human_required": "yellow",
        "ready": "yellow",
        "failed": "red",
        "failed_terminal": "red",
    }.get(result.status, "white")
    console.print(
        f"\n[bold]{result.ats}[/bold]  status [{status_colour}]{result.status}[/{status_colour}]"
    )
    if result.error:
        console.print(f"[red]{result.error}[/red]")

    if result.filled:
        table = Table(header_style="bold", title="Filled and verified")
        table.add_column("Field", overflow="fold")
        table.add_column("Result", style="dim", overflow="fold")
        for item in result.filled:
            mark = "[green]ok[/green]" if item["ok"] else "[red]FAILED[/red]"
            table.add_row(f"{mark} {item['label'][:44]}", item["detail"][:56])
        console.print(table)

    for label, items, colour in (
        ("Needs you", result.needs_user, "yellow"),
        ("Never filled by the agent", result.never_filled, "cyan"),
        ("Holding submission", result.holds, "red"),
    ):
        if items:
            console.print(f"\n[bold {colour}]{label}[/bold {colour}]")
            for item in items:
                console.print(f"  - {item}")

    if result.answers:
        console.print("\n[bold]Generated answers[/bold]")
        for answer in result.answers:
            mark = (
                "[green]ok[/green]" if answer["outcome"] == "answered" else "[yellow]you[/yellow]"
            )
            console.print(f"  {mark} {answer['question'][:60]}")
            if answer["text"]:
                console.print(f"      [dim]{answer['text'][:160]}[/dim]")

    if result.screenshots:
        console.print(f"\n[dim]Screenshots: {result.screenshots[-1]}[/dim]")
    if result.status == "awaiting_approval":
        console.print(
            f"\n[green]Ready.[/green] Submit with: careeragent approve {result.application_id}"
        )
    elif result.status == "human_required":
        console.print(
            f"\n[yellow]Over to you.[/yellow] Open it yourself with: "
            f"careeragent takeover {result.application_id}"
        )


@app.command()
def queue() -> None:
    """Applications waiting on you, best score first."""
    _setup()
    asyncio.run(_queue())


async def _queue() -> None:
    from .applications import tracker
    from .storage import init_db, session_scope
    from .storage import repository as repo

    await init_db()
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        items = await tracker.queue(session, row.id)

    if not items:
        console.print("[dim]Nothing waiting. Prepare one with 'careeragent apply <job-id>'.[/dim]")
        return

    table = Table(header_style="bold", title=f"{len(items)} waiting on you")
    table.add_column("Application")
    table.add_column("Score", justify="right")
    table.add_column("Role", overflow="fold")
    table.add_column("Company")
    table.add_column("Why", overflow="fold", style="dim")
    for item in items:
        table.add_row(
            item["application_id"][:8],
            f"{item['final_score']:.2f}" if item["final_score"] else "-",
            item["title"][:38],
            item["company"][:18],
            item["needs_attention"],
        )
    console.print(table)


@app.command()
def approve(
    application_id: Annotated[str, typer.Argument(help="Application id, from 'careeragent queue'")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt")] = False,
) -> None:
    """Submit an application you have reviewed.

    The pre-submit verdict still runs: a form with an unfilled required field is not
    submitted even with your approval.
    """
    _setup()
    asyncio.run(_approve(application_id, yes))


async def _approve(application_id: str, yes: bool) -> None:
    from .applications import service, tracker
    from .applications.service import ApplicationError
    from .storage import init_db, session_scope
    from .storage import repository as repo

    await init_db()
    async with session_scope() as session:
        detail = await tracker.timeline(
            session, await _resolve_application(session, application_id)
        )

    if not detail:
        console.print(f"[red]No application matching {application_id}.[/red]")
        raise typer.Exit(code=2)

    console.print(
        Panel(
            f"[bold]{detail['job']['title']}[/bold] at {detail['job']['company']}\n"
            f"{detail['url'] or 'no url'}\n"
            f"status: {detail['status']}   ats: {detail['ats']}",
            title="About to submit",
        )
    )
    if not yes and not typer.confirm("Submit this application?"):
        console.print("[dim]Not submitted.[/dim]")
        return

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            console.print("[red]No candidate.[/red] Run 'careeragent ingest-resume <file>' first.")
            raise typer.Exit(code=2)
        try:
            with _live_progress():
                result = await service.approve(session, detail["application_id"], candidate)
        except ApplicationError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

    if result.status == "applied":
        console.print("\n[bold green]Submitted.[/bold green]")
        console.print(f"[dim]Evidence: {result.evidence}[/dim]")
    elif result.status == "submission_unverified":
        console.print(
            "\n[yellow]Clicked submit, saw no confirmation.[/yellow] Check your email, then:\n"
            f"  careeragent status-set {result.application_id} applied"
        )
    else:
        console.print(f"\n[yellow]Not submitted — status {result.status}.[/yellow]")
        for hold in result.holds:
            console.print(f"  - {hold}")


async def _resolve_application(session, prefix: str) -> str:
    """Accept a short id prefix, the way git does.

    The board and the queue print eight characters, and retyping a full UUID from a
    terminal is the kind of friction that makes a tool go unused. An ambiguous or
    unknown prefix is returned unchanged so the caller reports "not found" rather
    than acting on the wrong application.
    """
    from sqlalchemy import select

    from .storage.models import Application

    if len(prefix) >= 36:
        return prefix

    rows = list(
        (await session.execute(select(Application.id).where(Application.id.startswith(prefix))))
        .scalars()
        .all()
    )
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        console.print(
            f"[yellow]{prefix!r} matches {len(rows)} applications.[/yellow] Use more characters."
        )
    return prefix


@app.command()
def takeover(
    application_id: Annotated[str, typer.Argument(help="Application id")],
) -> None:
    """Open a visible browser on the form and hand it to you.

    For a CAPTCHA, an MFA prompt, a login wall, or a question only you can answer.
    """
    _setup()
    asyncio.run(_takeover(application_id))


async def _takeover(application_id: str) -> None:
    from .applications import service
    from .applications.service import ApplicationError
    from .storage import init_db, session_scope
    from .storage import repository as repo

    await init_db()
    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            console.print("[red]No candidate.[/red] Run 'careeragent ingest-resume <file>' first.")
            raise typer.Exit(code=2)
        try:
            result = await service.takeover(session, application_id, candidate)
        except ApplicationError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

    _render_prepare(result)
    console.print(
        "\n[yellow]A browser window is open.[/yellow] Finish the form yourself, then:\n"
        f"  careeragent status-set {result.application_id} applied"
    )
    typer.prompt("Press Enter when you are done", default="", show_default=False)


@app.command(name="status-set")
def status_set(
    application_id: Annotated[str, typer.Argument()],
    status: Annotated[
        str, typer.Argument(help="applied | interview | offer | rejected_by_company | withdrawn")
    ],
    note: Annotated[str, typer.Option(help="Anything worth recording")] = "",
) -> None:
    """Record an outcome, or confirm a submission the agent could not verify."""
    _setup()
    asyncio.run(_status_set(application_id, status, note))


async def _status_set(application_id: str, status: str, note: str) -> None:
    from .applications import service
    from .applications.service import ApplicationError
    from .storage import init_db, session_scope

    await init_db()
    async with session_scope() as session:
        try:
            application = await service.set_status(session, application_id, status, note=note)
        except ApplicationError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
    console.print(f"[green]{application.status}[/green]")


@app.command()
def board() -> None:
    """The application tracker, as a board."""
    _setup()
    asyncio.run(_board())


async def _board() -> None:
    from .applications import tracker
    from .storage import init_db, session_scope
    from .storage import repository as repo

    await init_db()
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        data = await tracker.board(session, row.id)
        summary = await tracker.summary(session, row.id)

    if not data["total"]:
        console.print("[dim]No applications yet.[/dim]")
        return

    for column in data["columns"]:
        if not column["count"]:
            continue
        console.print(f"\n[bold]{column['name']}[/bold] ({column['count']})")
        for card in column["cards"][:12]:
            attention = (
                f"  [yellow]{card['needs_attention']}[/yellow]" if card["needs_attention"] else ""
            )
            console.print(
                f"  {card['application_id'][:8]}  {card['title'][:40]:42} "
                f"{card['company'][:18]:20}{attention}"
            )

    console.print(
        f"\n[bold]Submitted[/bold] {summary['submitted']}   "
        f"[bold]Responses[/bold] {summary['responses']}   "
        f"[bold]Interviews[/bold] {summary['interviews']}   "
        f"[bold]Offers[/bold] {summary['offers']}"
    )
    if summary["submission_unverified"]:
        console.print(
            f"[yellow]{summary['submission_unverified']} submission(s) unconfirmed[/yellow] "
            "— these are not counted as applied until you verify them."
        )


# ─── Companies and outreach ─────────────────────────────────────────────────


@app.command()
def companies(
    research: Annotated[int, typer.Option(help="Research this many unresearched companies")] = 0,
    limit: Annotated[int, typer.Option(help="Rows to show")] = 25,
) -> None:
    """Researched companies and their fit scores."""
    _setup()
    asyncio.run(_companies(research, limit))


async def _companies(research: int, limit: int) -> None:
    from sqlalchemy import select

    from . import company as company_mod
    from .storage import init_db, session_scope
    from .storage import repository as repo
    from .storage.models import Company

    await init_db()
    if research:
        # One short transaction per company, not one long one around the whole loop.
        #
        # This wrapped the entire crawl in a single `session_scope()`, which on SQLite
        # meant one write lock held for as long as sixty-six website reads take — minutes.
        # Every other process sharing the database got "database is locked" for the whole
        # run, which is how a click in the dashboard came back as a 500 while this was
        # going.
        #
        # It was also all-or-nothing: an error at company sixty would have rolled back the
        # fifty-nine profiles already gathered.
        async with session_scope() as session:
            candidate = await repo.load_candidate(session)
            pending = await repo.companies_needing_research(session, research)
            targets = [(row.id, row.name, row.domain) for row in pending]

        console.print(f"[bold]Researching {len(targets)} company website(s)...[/bold]")
        for company_id, name, domain in targets:
            # The network read happens outside any transaction. Holding one open across an
            # HTTP call is what turned a slow crawl into a lock on everybody else.
            profile = await company_mod.research(name, domain, candidate=candidate)
            if not profile.researched:
                console.print(f"  [dim]{name}: {profile.error}[/dim]")
                continue
            async with session_scope() as session:
                row = await repo.get_company(session, company_id)
                if row is not None:
                    await repo.save_company_profile(session, row, profile)
            console.print(
                f"  [green]{name}[/green] fit {profile.fit_score:.0%} "
                f"{profile.industry or ''} "
                f"[dim]{profile.fit_reasons[0] if profile.fit_reasons else ''}[/dim]"
            )

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(Company)
                    .where(Company.researched_at.is_not(None))
                    .order_by(Company.fit_score.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        total = len(await repo.companies_needing_research(session, 1000))

    if not rows:
        console.print(
            "[dim]No companies researched yet. Run 'careeragent companies --research 10'.[/dim]"
        )
        return

    table = Table(header_style="bold", title="Company fit")
    table.add_column("Fit", justify="right")
    table.add_column("Company", overflow="fold")
    table.add_column("Industry")
    table.add_column("AI", justify="right")
    table.add_column("Technology they name", overflow="fold", style="dim")
    for row in rows:
        table.add_row(
            f"{row.fit_score:.2f}" if row.fit_score else "-",
            row.name[:28],
            row.industry or "-",
            str(row.ai_relevance or "-"),
            ", ".join((row.tech_focus or [])[:6]) or "-",
        )
    console.print(table)
    if total:
        console.print(f"[dim]{total} more not yet researched.[/dim]")


@app.command()
def outreach(
    draft_for: Annotated[
        str, typer.Option("--draft", help="Company id to draft a message for")
    ] = "",
    limit: Annotated[int, typer.Option(help="Opportunities to list")] = 15,
) -> None:
    """Companies worth contacting, and the messages queued for them.

    Nothing here sends. A draft needs 'approve' and then 'send', and each one is a
    separate deliberate act.
    """
    _setup()
    asyncio.run(_outreach(draft_for or None, limit))


async def _outreach(draft_for: str | None, limit: int) -> None:
    from .applications import outreach as ox
    from .storage import init_db, session_scope
    from .storage import repository as repo

    await init_db()
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        candidate = await repo.load_candidate(session)
        if candidate is None and draft_for:
            console.print("[red]No candidate.[/red] Run 'careeragent ingest-resume <file>' first.")
            raise typer.Exit(code=2)

        if draft_for and candidate is not None:
            company = await repo.get_company(session, draft_for)
            if company is None:
                console.print(f"[red]No company with id {draft_for}.[/red]")
                raise typer.Exit(code=2)
            result = ox.draft(candidate, company)
            if not result.ok:
                console.print(f"[yellow]Not queued:[/yellow] {result.reason}")
                if result.body:
                    console.print(Panel(result.body, title="What it produced (rejected)"))
                return
            queued = await ox.create(session, row.id, company.id, result)
            console.print(
                Panel(f"[bold]{result.subject}[/bold]\n\n{result.body}", title=company.name)
            )
            console.print(
                f"\n[green]Queued[/green] {queued.id}\n"
                "[dim]Nothing has been sent. Approve it, then send it.[/dim]"
            )
            return

        data = await ox.board(session, row.id)
        opportunities = await ox.candidates_for_outreach(session, row.id, limit=limit)

    limits = data["limits"]
    console.print(
        f"[bold]Limits[/bold] {limits['sent_today']}/{limits['day_limit']} today, "
        f"{limits['sent_this_week']}/{limits['week_limit']} this week"
    )

    if data["total"]:
        for status, items in data["buckets"].items():
            console.print(f"\n[bold]{status}[/bold] ({len(items)})")
            for item in items[:10]:
                console.print(
                    f"  {item['id'][:8]}  {item['company'][:24]:26} {item['subject'][:44]}"
                )

    if opportunities:
        table = Table(header_style="bold", title="Worth contacting")
        table.add_column("Company id")
        table.add_column("Company", overflow="fold")
        table.add_column("Fit", justify="right")
        table.add_column("Contact?", overflow="fold", style="dim")
        for item in opportunities:
            table.add_row(
                item["company_id"][:8],
                item["name"][:26],
                f"{item['fit_score']:.2f}",
                "yes" if item["contactable"] else "no channel recorded",
            )
        console.print(table)
        console.print(
            "\n[dim]A company with no recorded channel needs one from a page they "
            "publish. This agent does not guess email addresses.[/dim]"
        )
    elif not data["total"]:
        console.print(
            "\n[dim]Nothing to contact yet. Research companies first: "
            "'careeragent companies --research 20'.[/dim]"
        )


# ─── Analytics and learning ─────────────────────────────────────────────────


@app.command()
def analytics(
    dimension: Annotated[
        str, typer.Option(help="role | location | company | ats | source | resume_archetype")
    ] = "role",
) -> None:
    """The funnel, the rates, and a breakdown — each with its sample size."""
    _setup()
    asyncio.run(_analytics(dimension))


async def _analytics(dimension: str) -> None:
    from . import analytics as an
    from .storage import init_db, session_scope
    from .storage import repository as repo

    await init_db()
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        counts = await an.funnel(session, row.id)
        rates = await an.rates(session, row.id)
        try:
            breakdown = await an.by_dimension(session, row.id, dimension)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from exc
        spend = await an.costs(session)

    table = Table(header_style="bold", title="Funnel")
    table.add_column("Stage")
    table.add_column("Count", justify="right")
    table.add_column("From previous", justify="right", style="dim")
    for stage in counts.to_dict()["stages"]:
        table.add_row(
            stage["stage"],
            str(stage["count"]),
            f"{stage['from_previous']:.0%}" if stage["from_previous"] else "-",
        )
    console.print(table)

    console.print("\n[bold]Rates[/bold]")
    for key in ("response", "interview", "offer"):
        rate = rates[key]
        if rate["rate"] is not None:
            console.print(f"  {key:10} {rate['rate']:.1%}  (n={rate['n']})")
        elif rate["n"]:
            console.print(
                f"  {key:10} {rate['count']}/{rate['n']} "
                f"[dim]— too few to call a rate (need {rates['min_sample']})[/dim]"
            )
        else:
            console.print(f"  {key:10} [dim]no submissions yet[/dim]")

    if breakdown["groups"]:
        table = Table(header_style="bold", title=f"By {dimension}")
        table.add_column(dimension.title(), overflow="fold")
        table.add_column("Applied", justify="right")
        table.add_column("Responses", justify="right")
        table.add_column("Rate", justify="right")
        for group in breakdown["groups"][:12]:
            rate = group["response"]
            table.add_row(
                str(group["key"])[:34],
                str(group["submitted"]),
                str(group["responses"]),
                f"{rate['rate']:.0%}" if rate["rate"] is not None else f"[dim]n={rate['n']}[/dim]",
            )
        console.print(table)

    console.print(
        f"\n[bold]LLM spend[/bold] ${spend['total_usd']:.4f} over {spend['window_days']}d, "
        f"{spend['calls']} call(s), {spend['cache_hits']} cached"
        + (
            f", ${spend['cost_per_application_usd']:.4f}/application"
            if spend["cost_per_application_usd"]
            else ""
        )
    )


@app.command()
def learn(
    propose_change: Annotated[
        bool, typer.Option("--propose", help="Also propose a weight change")
    ] = False,
) -> None:
    """What the outcomes show, and optionally a proposed ranking change.

    Nothing is applied. At one person's application volumes this is a decision aid,
    not an optimizer, and it says so.
    """
    _setup()
    asyncio.run(_learn(propose_change))


async def _learn(propose_change: bool) -> None:
    from . import learning
    from .storage import init_db, session_scope
    from .storage import repository as repo

    await init_db()
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        items = await learning.observations(session, row.id)
        proposal = await learning.propose_weights(session, row.id) if propose_change else None
        if proposal is not None:
            await learning.save_proposal(session, row.id, proposal)

    console.print("[bold]Observations[/bold]")
    for item in items:
        console.print(f"  - {item['text']}")

    if proposal is None:
        return

    console.print(
        f"\n[bold]Proposal[/bold] ({proposal.confidence} confidence, n={proposal.sample_size})"
    )
    console.print(f"  {proposal.observation}")
    if not proposal.actionable:
        console.print("  [dim]Nothing to change.[/dim]")
        return
    for change in proposal.diff():
        console.print(
            f"  {change['key']:24} {change['from']} -> {change['to']} ({change['delta']:+.3f})"
        )
    console.print(f"\n[dim]{proposal.rationale}[/dim]")
    console.print(
        "[dim]Nothing has changed. Apply it from the dashboard or via "
        "POST /api/v1/learning/proposals/<id>/apply.[/dim]"
    )


@app.command()
def goal(
    text: Annotated[str, typer.Argument(help="What you want, in a sentence.")],
    steps: Annotated[int, typer.Option(help="Maximum steps the supervisor may take.")] = 6,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show the plan without spending API quota or opening a browser.",
        ),
    ] = False,
) -> None:
    """Give the supervisor a goal and let it decide the steps.

    Unlike every other command here, the order of operations is not fixed — the supervisor
    chooses each step from what the last one found. It can queue and prepare applications;
    it cannot submit one, so nothing leaves your machine as a result of this.
    """
    _setup()
    asyncio.run(_goal(text, steps, dry_run))


async def _goal(text: str, steps: int, dry_run: bool) -> None:
    from .graph.supervisor import EXPLAIN, run_supervisor
    from .storage import init_db, session_scope
    from .storage import repository as repo

    await init_db()
    async with session_scope() as session:
        if await repo.load_candidate(session) is None:
            console.print("[red]No resume loaded.[/red] Run 'careeragent ingest-resume <file>'.")
            raise typer.Exit(code=2)

    console.print(f"[bold]{text}[/bold]")
    if dry_run:
        console.print("[dim]Dry run: discovery and form filling are skipped.[/dim]")
    console.print()

    state = await run_supervisor(text, max_steps=steps, dry_run=dry_run)

    plan = state.get("plan") or []
    if plan:
        table = Table(header_style="bold", title="The path it chose")
        table.add_column("#", justify="right")
        table.add_column("Step")
        table.add_column("Why", overflow="fold", style="dim")
        for i, entry in enumerate(plan, 1):
            table.add_row(str(i), entry.get("step", ""), entry.get("reason", "")[:110])
        console.print(table)

    results = Table(header_style="bold", title="What each step returned")
    results.add_column("Step")
    results.add_column("Result", overflow="fold", style="dim")
    for entry in state.get("history") or []:
        results.add_row(
            str(entry.get("step") or ""), _json.dumps(entry.get("result"), default=str)[:150]
        )
    console.print(results)

    parked = state.get("awaiting_approval") or []
    shortlisted = state.get("shortlisted") or []
    console.print(f"\n[bold]{state.get('summary', '')}[/bold]")
    if reason := state.get("stopped_because"):
        console.print(f"[dim]{EXPLAIN.get(reason, reason)}[/dim]")
    if shortlisted:
        console.print(f"Queued {len(shortlisted)} new opening(s).")
    if parked:
        console.print(
            f"[yellow]{len(parked)} application(s) are filled and waiting for you.[/yellow] "
            "Review with 'careeragent queue', then 'careeragent approve <id>'."
        )
    # Said plainly whether or not anything was prepared: the absence of a submission is the
    # property worth being sure of.
    console.print("[dim]Nothing was submitted.[/dim]")
    if err := state.get("error"):
        console.print(f"[red]{err}[/red]")


@app.command()
def rescore(
    apply: Annotated[
        bool, typer.Option("--apply", help="Write the new scores. Without it, nothing changes.")
    ] = False,
    limit: Annotated[int, typer.Option(help="Only the first N, for a quick look.")] = 0,
) -> None:
    """Recompute stored job scores against the current scorer.

    Reads what is already in the database — no network, no LLM spend, nothing re-fetched.
    Embedding and LLM scores are preserved, since neither depends on skill matching and the
    second one cost money.

    Shows what would change and stops. Pass --apply to write.
    """
    _setup()
    asyncio.run(_rescore(apply, limit))


async def _rescore(apply: bool, limit: int) -> None:
    from .pipeline.rescore import rescore as run_rescore
    from .storage import init_db, session_scope
    from .storage import repository as repo

    await init_db()
    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            console.print("[red]No resume loaded.[/red] Run 'careeragent ingest-resume <file>'.")
            raise typer.Exit(code=2)
        row = await repo.get_or_create_candidate(session)
        report = await run_rescore(
            session, candidate, row.id, dry_run=not apply, limit=limit or None
        )

    console.print(f"[bold]{report.summary()}[/bold]")
    if not report.biggest:
        console.print("[dim]Nothing moved by more than a rounding difference.[/dim]")
        return

    table = Table(header_style="bold", title="Biggest moves")
    table.add_column("Role", overflow="fold")
    table.add_column("Company")
    table.add_column("Before", justify="right")
    table.add_column("After", justify="right")
    table.add_column("Why", overflow="fold", style="dim")
    for change in report.biggest:
        why = change.reason
        arrow = "[green]up[/green]" if change.delta > 0 else "[yellow]down[/yellow]"
        table.add_row(
            change.title[:40],
            change.company[:18],
            f"{change.before:.3f}",
            f"{change.after:.3f} {arrow}",
            why,
        )
    console.print(table)

    if not apply:
        console.print("\n[dim]Nothing was written. Re-run with --apply to save these.[/dim]")


@app.command()
def relink(
    apply: Annotated[
        bool, typer.Option("--apply", help="Write the links. Without it, nothing changes.")
    ] = False,
) -> None:
    """Attach stored jobs to the company rows they name.

    Worth running after researching a company: its jobs were probably harvested before the
    company row existed, and until they are linked, nothing that hangs off the company —
    people, outreach history, research — can be reached from the job.

    Matches on the normalized name only. Nothing is created; a job whose company is not in
    the database stays unlinked, which is the true state.
    """
    _setup()
    asyncio.run(_relink(apply))


async def _relink(apply: bool) -> None:
    from .storage import init_db, session_scope
    from .storage import repository as repo

    await init_db()
    async with session_scope() as session:
        report = await repo.link_jobs_to_companies(session, dry_run=not apply)

    verb = "Linked" if apply else "Would link"
    console.print(
        f"[bold]{verb} {report['linked']} of {report['unlinked']} unlinked job(s).[/bold]"
    )
    if report["still_unlinked"]:
        console.print(
            f"[dim]{report['still_unlinked']} name no company in the database. "
            "Research them and run this again.[/dim]"
        )
    if not apply and report["linked"]:
        console.print("\n[dim]Nothing was written. Re-run with --apply to save.[/dim]")


# ─── Accounts ───────────────────────────────────────────────────────────────

account_app = typer.Typer(help="Accounts and passwords. Local only; never over the network.")
app.add_typer(account_app, name="account")


@account_app.command("list")
def account_list() -> None:
    """Every account, and whether it can be logged into."""
    _setup()
    asyncio.run(_account_list())


async def _account_list() -> None:
    from sqlalchemy import select

    from .storage import init_db, session_scope
    from .storage.models import Candidate, User

    await init_db()
    async with session_scope() as session:
        users = (await session.execute(select(User).order_by(User.created_at))).scalars().all()
        owned = {
            str(user_id): str(candidate_id)
            for candidate_id, user_id in (
                await session.execute(select(Candidate.id, Candidate.user_id))
            ).all()
            if user_id
        }

    if not users:
        console.print("[yellow]No accounts.[/yellow] Create one with 'careeragent account create'.")
        return

    table = Table(header_style="bold")
    table.add_column("Email", overflow="fold")
    table.add_column("Name")
    table.add_column("Can log in", justify="center")
    table.add_column("Profile", justify="center")
    table.add_column("Last login", style="dim")
    for user in users:
        table.add_row(
            user.email,
            user.display_name or "",
            "[green]yes[/green]" if user.password_hash else "[yellow]no password[/yellow]",
            "yes" if owned.get(user.id) else "[dim]none[/dim]",
            user.last_login_at.strftime("%Y-%m-%d %H:%M") if user.last_login_at else "never",
        )
    console.print(table)


@account_app.command("create")
def account_create(
    email: Annotated[str, typer.Argument(help="The address to sign in with.")],
    claim: Annotated[
        bool,
        typer.Option(
            "--claim/--no-claim",
            help="Attach the existing unowned profile to this account, if there is one.",
        ),
    ] = False,
) -> None:
    """Create an account. The password is prompted for, never passed as an argument.

    An argument would be in the shell history, in the process list while it runs, and in
    any terminal recording — three places a password should not be.
    """
    _setup()
    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    asyncio.run(_account_create(email, password, claim))


async def _account_create(email: str, password: str, claim: bool) -> None:
    from sqlalchemy import select

    from .auth import service
    from .auth.passwords import WeakPassword
    from .storage import init_db, session_scope
    from .storage.models import Candidate

    await init_db()
    async with session_scope() as session:
        try:
            user = await service.register(session, email, password)
        except WeakPassword as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from exc
        except service.AuthError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from exc

        message = f"Created {user.email}."
        if claim:
            orphan = (
                (
                    await session.execute(
                        select(Candidate).where(Candidate.user_id.is_(None)).limit(2)
                    )
                )
                .scalars()
                .all()
            )
            if len(orphan) == 1:
                orphan[0].user_id = user.id
                message += f" Claimed the existing profile for {orphan[0].name}."
            elif len(orphan) > 1:
                # Refusing rather than picking. Attaching the wrong profile hands somebody
                # another person's resume, applications and outreach history.
                message += " [yellow]More than one unowned profile — claimed none.[/yellow]"
            else:
                message += " No unowned profile to claim."

    console.print(f"[green]{message}[/green]")


@account_app.command("set-password")
def account_set_password(
    email: Annotated[str, typer.Argument(help="Which account.")],
) -> None:
    """Set or reset a password locally.

    This is how the account created by the tenancy migration becomes usable. That account
    deliberately has no password — inventing one would have meant shipping it in a migration
    file — so without this command, upgrading to multi-user mode would lock you out of your
    own corpus: every endpoint needs a token, and a token needs a login.

    Every existing session for the account is revoked, because a password reset that leaves
    old tokens working is not a reset.
    """
    _setup()
    password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    asyncio.run(_account_set_password(email, password))


async def _account_set_password(email: str, password: str) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import func, select

    from .auth import passwords
    from .auth.passwords import WeakPassword
    from .storage import init_db, session_scope
    from .storage.models import User

    await init_db()
    async with session_scope() as session:
        user = (
            await session.execute(
                select(User).where(func.lower(User.email) == email.strip().lower())
            )
        ).scalar_one_or_none()
        if user is None:
            console.print(f"[red]No account for {email}.[/red] See 'careeragent account list'.")
            raise typer.Exit(code=2)
        try:
            user.password_hash = passwords.hash_password(password)
        except WeakPassword as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from exc
        user.token_serial = int(user.token_serial or 0) + 1
        user.tokens_valid_from = datetime.now(UTC)

    console.print(f"[green]Password set for {email}.[/green] Existing sessions are revoked.")


@account_app.command("secret")
def account_secret() -> None:
    """Print a signing secret to paste into .env. Generated here, stored nowhere.

    Written out rather than saved because this process should not be the thing that decides
    where a deployment keeps its keys — and a secret that appears in a file the tool wrote
    is one nobody remembers to rotate.
    """
    import secrets as _secrets

    console.print(f"CAREERAGENT_AUTH_SECRET={_secrets.token_urlsafe(48)}")


# ─── Files on disk ──────────────────────────────────────────────────────────

storage_app = typer.Typer(help="Resumes, renders and screenshots: what is stored and where.")
app.add_typer(storage_app, name="storage")

# The three prefixes anything is ever written under. Named here rather than discovered, so
# `prune` cannot be pointed at a directory it was not meant to touch.
PREFIXES = ("resumes", "renders", "applications")


@storage_app.command("usage")
def storage_usage() -> None:
    """What is stored, how much of it, and which backend holds it."""
    _setup()
    asyncio.run(_storage_usage())


async def _storage_usage() -> None:
    from .storage.objects import get_store

    store = get_store()
    table = Table(header_style="bold", title="Object store")
    table.add_column("Prefix")
    table.add_column("Objects", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Oldest", style="dim")

    total_bytes = 0
    for prefix in PREFIXES:
        objects = await store.list(prefix)
        size = sum(o.size_bytes for o in objects)
        total_bytes += size
        oldest = min((o.modified_at for o in objects), default=None)
        table.add_row(
            prefix,
            str(len(objects)),
            _human_bytes(size),
            oldest.strftime("%Y-%m-%d") if oldest else "—",
        )

    console.print(table)
    console.print(f"[bold]{_human_bytes(total_bytes)}[/bold] in {store.describe()}")


def _human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


@storage_app.command("prune")
def storage_prune(
    older_than_days: Annotated[
        int, typer.Option("--older-than", help="Delete objects older than this many days.")
    ] = 90,
    keep: Annotated[
        int, typer.Option(help="Always keep this many newest per prefix, whatever their age.")
    ] = 20,
    apply: Annotated[
        bool, typer.Option("--apply", help="Actually delete. Without it, nothing is removed.")
    ] = False,
) -> None:
    """Remove old screenshots and renders.

    Screenshots are the evidence a submission happened, so the defaults are conservative and
    `--keep` is a floor that age cannot override: "delete everything older than 90 days" on
    a system nobody has used for 91 days would delete the only evidence it has.

    Resumes are never pruned. A master resume is the input to everything else in the system
    and is not regenerable from anything here.
    """
    _setup()
    asyncio.run(_storage_prune(older_than_days, keep, apply))


async def _storage_prune(older_than_days: int, keep: int, apply: bool) -> None:
    from datetime import UTC, datetime, timedelta

    from .storage.objects import get_store

    store = get_store()
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)

    total = 0
    for prefix in PREFIXES:
        if prefix == "resumes":
            continue  # never; see the docstring above
        if apply:
            removed = await store.prune(prefix, older_than_days=older_than_days, keep_at_least=keep)
        else:
            objects = sorted(await store.list(prefix), key=lambda o: o.modified_at, reverse=True)
            removed = sum(1 for o in objects[keep:] if o.modified_at < cutoff)
        total += removed
        verb = "removed" if apply else "would remove"
        console.print(f"  {prefix:<14} {verb} {removed}")

    if not total:
        console.print("[dim]Nothing old enough to remove.[/dim]")
    elif not apply:
        console.print(f"\n[dim]{total} object(s). Nothing was deleted — re-run with --apply.[/dim]")


@app.command()
def monitor(
    once: Annotated[
        bool, typer.Option("--once", help="Sweep now and exit, instead of showing status.")
    ] = False,
    limit: Annotated[int, typer.Option(help="Boards to read this sweep. 0 = configured size.")] = 0,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be found without storing it.")
    ] = False,
) -> None:
    """Watch company job boards for new openings.

    Without --once this shows what is being watched and when each board is next due.

    Each board keeps its own cadence: halved when it produces something new, lengthened when
    it does not, between 6 hours and a week. A company that posts weekly gets checked often;
    one that posts yearly is left alone.

    New openings are scored deterministically. Nothing is applied to.
    """
    _setup()
    asyncio.run(_monitor(once, limit, dry_run))


async def _monitor(once: bool, limit: int, dry_run: bool) -> None:
    from .monitoring import status as monitor_status
    from .monitoring import sweep
    from .storage import init_db, session_scope
    from .storage import repository as repo

    await init_db()

    if not once:
        async with session_scope() as session:
            state = await monitor_status(session)
        table = Table(header_style="bold", show_header=False, title="Career monitoring")
        table.add_column("", style="bold")
        table.add_column("")
        table.add_row("Background loop", "on" if state["enabled"] else "[dim]off[/dim]")
        table.add_row("Boards watched", str(state["boards_watched"]))
        table.add_row("Due now", str(state["boards_due_now"]))
        if state["boards_given_up_on"]:
            table.add_row("Given up on", f"[yellow]{state['boards_given_up_on']}[/yellow]")
        low, high = state["interval_band_hours"]
        table.add_row("Check every", f"{low}-{high} hours, per board")
        console.print(table)

        if state["next_checks"]:
            upcoming = Table(header_style="bold", title="Next up")
            upcoming.add_column("Board")
            upcoming.add_column("Every", justify="right")
            upcoming.add_column("New seen", justify="right")
            upcoming.add_column("Due", style="dim")
            for item in state["next_checks"]:
                upcoming.add_row(
                    f"{item['provider']}/{item['slug'][:24]}",
                    f"{item['every_hours']}h",
                    str(item["new_jobs_seen"]),
                    (item["at"] or "")[:16].replace("T", " "),
                )
            console.print(upcoming)

        if not state["enabled"]:
            console.print(
                "\n[dim]The background loop is off. Run 'careeragent monitor --once' to sweep "
                "now, or set CAREERAGENT_MONITOR_ENABLED=true to have the server do it.[/dim]"
            )
        console.print("[dim]Sweep now with: careeragent monitor --once[/dim]")
        return

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            console.print("[red]No resume loaded.[/red] Run 'careeragent ingest-resume <file>'.")
            raise typer.Exit(code=2)
        row = await repo.get_or_create_candidate(session)
        report = await sweep(session, candidate, row.id, limit=limit or None, dry_run=dry_run)

    console.print(f"[bold]{report.summary()}[/bold]")

    best = report.best_new
    if best:
        table = Table(header_style="bold", title="New openings")
        table.add_column("Score", justify="right")
        table.add_column("Role", overflow="fold")
        table.add_column("Company")
        table.add_column("Location", overflow="fold")
        for job in best[:20]:
            score = job.get("score")
            table.add_row(
                f"{score:.2f}" if score is not None else "-",
                str(job.get("title", ""))[:40],
                str(job.get("company", ""))[:20],
                str(job.get("location", ""))[:22],
            )
        console.print(table)

    failed = [r for r in report.results if r.error]
    if failed:
        console.print(f"\n[yellow]{len(failed)} board(s) could not be read:[/yellow]")
        for result in failed[:5]:
            console.print(f"  {result.provider}/{result.slug[:28]} — {result.error[:70]}")

    if dry_run:
        console.print("\n[dim]Dry run: nothing was stored.[/dim]")
    console.print("[dim]Nothing was applied to.[/dim]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
