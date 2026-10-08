# AI Career Agent

A personal job-search and application assistant. Not a mass-apply bot — the design goal
is to search widely, understand each opportunity, apply selectively, and never make a
claim about you that isn't backed by evidence.

**Works with any resume, in any field.** Upload a CV — civil engineering, pharma,
finance, software — and it works out what you do from the evidence, not the job title at
the top. Pick job families and a region, and five agents find the companies hiring there,
resolve each one's own application system, and rank what is open. Tick the companies you
want and three browser agents work the queue in parallel: tailoring a resume per company,
filling the form, and stopping for your approval before anything is sent.

Every claim is traceable to a line you wrote. Nothing is reported as submitted without
evidence that it was.

Design docs: [research](docs/00-research.md) · [architecture](docs/01-architecture.md) · [roadmap](docs/02-roadmap.md)

---

## Quick start

```bash
python -m venv job
job\Scripts\activate                  # Windows;  source job/bin/activate on POSIX
pip install -r requirements-dev.txt
playwright install chromium
pip install -e .

careeragent migrate                   # create or update the schema
careeragent ingest-resume my_cv.pdf   # your resume -> Fact Bank
uvicorn careeragent.api:app --reload  # then open http://127.0.0.1:8000
```

The dashboard opens on **Campaign**, which is the flow:

```
your resume  →  detected field  →  pick job families  →  pick regions
             →  COMPANIES hiring there, with their ATS resolved
             →  tick the ones you want  →  Apply
             →  3 agents tailor + fill in parallel
             →  approve each one as it parks
```

Or from the terminal:

```bash
careeragent discover --segment india --roles "Data Scientist" --llm
careeragent jobs                      # ranked results
careeragent tailor <job-id>           # a job-specific resume, validated
careeragent apply <job-id>            # fill the form and stop
careeragent queue                     # what is waiting on you
careeragent approve <application-id>  # submit one you have reviewed
```

**No API key and no database setup required** — but the free ones change the answer a
lot. Without them the agent reads three keyless job boards that cover remote roles well
and cities poorly, so a Bangalore search returns tens of companies instead of hundreds.

| Key | Free tier | Why it matters | Get it |
|---|---|---|---|
| **Adzuna** | ~hundreds of calls/day | Proper India coverage; returns a structured employer name on every row. The single highest-value key. | [developer.adzuna.com](https://developer.adzuna.com/signup) — instant, no card |
| **JSearch** | 200 calls/**month** | Google Jobs index: reaches LinkedIn/Indeed/Naukri postings *through Google* rather than by scraping them. | [RapidAPI](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) |
| **Jooble** | no published cap | Breadth once the metered sources are spent. | [jooble.org/api/about](https://jooble.org/api/about) |

Put them in `.env` (see `.env.example`). The dashboard shows which are live and links to
the signup for the rest. Everything else — scoring, tailoring, form filling, the ATS
readers — needs no key at all.

### The dashboard

```bash
uvicorn careeragent.api:app --reload
```

Then open **http://127.0.0.1:8000** — upload a resume, run a search, watch the live
activity feed, review what the agent filled and what it refused to fill, and approve
applications one at a time. API docs are at `/docs`.

Run it single-worker if you use the approval flow: a held browser session belongs to one
process, and with several workers an approval will silently re-open the form instead of
submitting the one you reviewed.

### Optional: Postgres + pgvector

```bash
docker compose up -d
# then set in .env:
# CAREERAGENT_DATABASE_URL=postgresql+psycopg://careeragent:careeragent@localhost:5433/careeragent
careeragent migrate
```

Port **5433**, because a native PostgreSQL already owns 5432 on this machine. The
container image ships `pgvector` precompiled, which is the whole reason to use it rather
than a local install.

---

## What it does

| Area | State |
|---|---|
| **Any-field resumes** — 50 job families across Technology, Engineering, Business, Design, Healthcare; evidence-based field detection | ✅ civil, pharma, finance, design all tested |
| **Company discovery by region** — 6 aggregators, recruiter filtering, cross-source deduplication | ✅ 3 keyless, 3 free-key |
| **ATS resolution from a company name** — domain lookup → careers page → board slug → real openings | ✅ this is what makes a lead applyable |
| **Campaign graph** — 5 LangGraph agents, parallel `Send()` fan-out over families × sources, then over companies | ✅ |
| **Apply graph** — 5 agents, `interrupt()` for approval, three modes | ✅ |
| **Durable checkpoints** — SQLite or Postgres, following the application database | ✅ a parked approval survives a restart, verified across two processes |
| **Career Supervisor** — a goal in a sentence; it chooses each step from what the last one found | ✅ no submit step exists, so nothing it prepares can send itself |
| **Tool layer** — 11 tools, 7 per-agent toolsets, effects declared and enforced | ✅ zero tools reach outside this machine |
| **Tool-calling runtime** — turn and call limits, per-turn budget checks, model fallthrough | ✅ a tool failure is fed back for the agent to correct |
| **Versioned prompts** — one shared copy of the anti-fabrication rules, content-addressed versions | ✅ recorded against every model-made decision |
| **Decision trail** — why each agent chose what it chose, append-only, readable in the UI | ✅ 6 decision points recorded |
| **Parallel apply pool** — 3 concurrent browser agents, per-item daily cap, one-at-a-time per employer | ✅ |
| **Discovery** — 14 ATS readers incl. **Keka** and **Freshteam** for Indian companies, careers-page slug resolution, concurrent fetch with backoff | ✅ verified against live boards |
| **Normalization + dedup** — one opening on three boards becomes one job, three listings | ✅ |
| **Stage 1 scoring** — deterministic, explainable, gates LLM spend | ✅ |
| **Stage 2 scoring** — semantic similarity, on-device, calibrated | ✅ no API key |
| **Stage 3 scoring** — LLM reasoning: strengths, gaps, concerns, strategy | ✅ `--llm` |
| **Company intelligence** — reads public pages, scores company fit on its own axis | ✅ robots.txt honoured |
| **Ranking + decision engine** — configurable versioned weights, apply/review/outreach/reject | ✅ |
| **Resume → Fact Bank** — PDF/DOCX/TXT, every fact traced to its source line | ✅ |
| **Resume tailoring** — 8 archetypes, evidence-bound selection, optional LLM rephrasing | ✅ works with no LLM |
| **Hybrid agents** — the validator vetoes, the agent revises once, the validator decides again | ✅ 4 agents: resumes, answers, letters, outreach |
| **Evidence validator** — fabricated skills, overstated duration, renamed employers, shifted dates | ✅ pure, exhaustively tested |
| **ATS validator + PDF render** — single-column, real text, refuses an unvalidated document | ✅ |
| **Cover letters** — grounded in facts and stored research, rejected if not | ✅ no fallback by design |
| **Application answers** — grounded, with `REQUIRES_USER` as a first-class outcome | ✅ |
| **Question memory** — reused across applications, re-checked before reuse | ✅ |
| **Browser engine** — canonical field catalog, evidence-bound resolver, pre-submit verdict, 6 ATS adapters | ✅ |
| **Application pipeline** — shortlist → tailor → fill → approve → submit → track | ✅ |
| **Approval queue + live view** — what was filled, what was refused, what is holding it, screenshots | ✅ |
| **Human takeover** — a visible browser for CAPTCHA, MFA, or a question only you can answer | ✅ |
| **Tracker** — state machine, Kanban, timeline, follow-ups, duplicate guard at the DB level | ✅ |
| **Outreach** — targeting, grounded drafts, per-message approval, rate limits in code | ✅ no send transport |
| **Analytics** — funnel, rates with sample sizes, breakdowns, cost per application | ✅ |
| **Learning** — descriptive observations and weight *proposals*, never auto-applied | ✅ |
| **Web dashboard** — 14 screens, live SSE feed, no build step | ✅ |
| **HTTP API** — 104 operations across 13 domains | ✅ |
| **Preferences** — experience band, work mode, employment type, locations, job age | ✅ a junior band removes senior roles rather than ranking them lower |
| **Career monitoring** — re-reads company boards on an adaptive schedule, scores what is new | ✅ off by default; cannot apply to anything |
| **Knowledge graph** — candidate, skills, jobs, companies and applications, projected from the tables | ✅ no second store, so it cannot go stale |
| **Graph questions** — never-contacted companies, skills that would open doors, reach per skill | ✅ |
| **Rescore in place** — recompute stored scores after a scorer change | ✅ no network, no LLM spend, dry run by default |
| **Migrations** — Alembic, with adoption for a pre-migration database | ✅ |

```
870 tests, offline        15 browser tests against recorded form fixtures
ruff: clean               mypy: clean, 115 files
```

---

## The agents

Two LangGraph graphs, five agents each. A graph earns its place in both, for different
reasons.

**Campaign** — discovery. The fan-outs are the point: sourcing is a map over
(family × region × source) and enrichment is a map over companies, and both merge
concurrent writes into one state through reducers rather than hand-written merge code.

```
START → ① profile ──Send()──► ② sourcing × N   ‖ parallel, budgeted per source
                                    ▼
                              ③ aggregate  ──Send()──► ④ enrichment × N  ‖ parallel
                                                              ▼
                                                        ⑤ rank → END
```

**Apply** — one application. Linear, but `interrupt()` means an approval can sit for days
and resume at the submit step rather than re-tailoring and re-opening the form.

```
START → ① tailor → ② answer → ③ form → ④ verify ─┬─ interrupt ─→ ⑤ submit → END
                                                  └─ needs a human ────────→ END
```

The one thing deliberately *outside* graph state is the live browser page. An approval is
only meaningful if it approves the form you looked at, and a page cannot be serialized
into any checkpointer — so it is held in a session registry with a TTL, and if it expires
the submit step re-prepares and says so rather than submitting something unseen.

---

## Four design rules worth knowing

**Nothing is claimed without evidence.** Every fact in the Fact Bank records the resume
line it came from. A tailored resume is an *ordering over those facts*, never a new
document — so a variant structurally cannot contain anything your master resume doesn't.
Between generation and output sits a pure validator, and it is not advisory: a document
with any evidence violation is stored for you to inspect but never rendered to PDF, so it
cannot be attached to anything.

It catches real things. On a live run against a stored job, Gemini's tailored summary
claimed **"2 years of experience" against a Fact Bank supporting 0.2**. The rewrite was
discarded, the deterministic version was used instead, and the dashboard said so.

**The agent fails safe, mechanically.** Demographics, sponsorship declarations and access
codes are never auto-filled at any autonomy level — and no override, approval or setting
can reach them, because the sensitivity check runs before the code that applies overrides.
Every write is read back and verified. A dropdown whose options can't express a value is
escalated to you rather than rounded. A pure `presubmit_verdict()` decides whether a form
is safe to submit, and it runs with no browser at all, so it is tested exhaustively.

**Evidence or it didn't happen.** `APPLIED` requires a post-submit signal; the state
machine refuses the transition without one. A clicked submit with no confirmation is
`SUBMISSION_UNVERIFIED` — a first-class state that surfaces as "please check this went
through", because quietly counting it as applied would corrupt every downstream number
and could cause a duplicate application later.

**Numbers carry their sample size.** A response rate over three applications is noise.
Below eight submissions the analytics return the raw fraction and `rate: null`, and the
learning engine says it doesn't know yet rather than proposing a weight change. With
50–200 applications this is a decision aid, not an optimizer, and it says so.

---

## Autonomy

Default is **level 4**: prepare everything, queue it, submit on your explicit approval.

| Level | Grants |
|---|---|
| 0–1 | Search, score, rank, research companies |
| 2 | + tailor resumes, draft answers |
| 3 | + open a browser and fill forms; never submits |
| 4 | + submit on your approval (**default**) |
| 5 | + auto-submit above the threshold, on allow-listed ATS platforms only |

**Always human, at every level, with no setting that changes it:** CAPTCHA, MFA,
identity verification, login walls, demographic questions, and work-authorisation or
sponsorship declarations.

Approving does not force a submit. The page is re-read and the pre-submit verdict runs
again, so a field that went blank in the meantime still stops it.

Within level 4, a campaign picks how far each application goes on its own:

| Mode | Behaviour |
|---|---|
| `preview` | Fill, show you what was filled and what was refused, submit on a second click. **Default.** |
| `one_click` | Clicking Apply was the approval. Submits without a second confirmation. |
| `threshold` | Submits above a match score, queues the rest. |

No mode can reach the hard stops — they are refused a layer below, where no argument to
any function can move them.

---

## Why ATS feeds, not job portals

Companies publish these endpoints so their own careers pages can render them. They are
unauthenticated, structured, and carry no prohibition on automated access. LinkedIn,
Indeed, Naukri and Glassdoor all prohibit it — so those adapters are isolated, off by
default, and nothing here depends on them. See [docs/02-roadmap.md](docs/02-roadmap.md) §4.1.

The same reasoning shapes outreach: contacts are recorded only from a page the company
publishes or from you, `/send` marks a message as sent rather than delivering mail on
your behalf, and LinkedIn is draft-only because automated messaging there risks the
account you need most.

---

## Known limitations

Stated rather than designed around.

- **Without the three free keys, regional search is thin.** The keyless sources are
  remote-job boards; a Bangalore campaign against them returns remote-work marketplaces,
  not Bangalore employers. This is the single biggest determinant of usefulness and the
  dashboard says so on the Campaign screen rather than returning a short list silently.
- **A company is only applyable if its ATS can be resolved.** Roughly a third resolve in
  practice; the rest stay listed as leads with their original posting link. A careers
  page rendered entirely in JavaScript has no link to find.
- **Company fit is weak for consumer-facing companies.** It reads public pages, and a
  consumer fintech's marketing site names no engineering stack — so it scores low on
  technology overlap while a content-rich engineering site scores well. That is a
  property of the evidence, not of the employer. A genuinely unreadable (JavaScript-only)
  page is detected separately and excluded rather than scored zero.
- **Adapters need periodic re-recording.** ATS forms change without notice. The fixtures
  carry a capture date so staleness is visible; budget for maintenance.
- **Workday is discovery-only.** Applying requires creating an account per tenant with
  email verification. That is a human step by design.
- **The approval flow is single-process.** A held browser session cannot be handed to
  another worker.
- **No send transport for outreach.** Wiring SMTP to send on your behalf is your
  decision, not a default.
- **Single candidate.** `candidate_id` is threaded throughout, but auth and tenancy
  isolation are not built.

---

## Development

```bash
pytest                      # 870 tests, offline
pytest -m browser           # form fixtures replayed in Chromium, offline
pytest -m live              # smoke tests against real ATS boards
ruff check backend tests && ruff format backend tests
mypy backend
careeragent migrate --show  # migration state and history
```

Third-party attribution: [NOTICE](NOTICE).
