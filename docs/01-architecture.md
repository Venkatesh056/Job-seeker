# System Architecture — AI Career Agent

Companion to `00-research.md`. Everything here is shaped by four findings from that research:
ATS feeds are the reliable discovery channel and slug resolution is the real work; portal
automation is a ToS liability that must be isolatable; silent-wrong is the dominant failure
mode; and a canonical field vocabulary is what keeps ATS adapters small.

---

## 1. Architectural position

The brief asks for an "AI career operating system". Concretely that means three subsystems with
very different reliability characteristics, and the architecture's main job is to keep them from
contaminating each other:

| Subsystem | Nature | Failure mode | Design response |
|---|---|---|---|
| **Discovery + Intelligence** | Batch, idempotent, cheap to retry | Missing or stale jobs | Pure functions over a normalized schema; re-runnable; cached |
| **Application execution** | Stateful, side-effecting, irreversible | Wrong data submitted, or a false "applied" | Verify-after-act, human gates, evidence required, never silent |
| **Outreach** | Side-effecting, reputational, irreversible | Spam, damaged relationships | Approval-only, rate-limited, no autonomy level unlocks it |

A single "agent loop" spanning all three would let a discovery retry re-trigger a submission.
They are separated by a durable queue and a state machine, not by function calls.

---

## 2. Recommended stack

Largely as the brief proposes, with four reasoned deviations.

| Layer | Choice | Notes |
|---|---|---|
| Frontend | Next.js (App Router) + TypeScript + Tailwind + shadcn/ui | As briefed. Server components for read views, SSE for the live feed. |
| Backend | Python 3.12 + FastAPI + Pydantic v2 | As briefed. Pydantic models are the normalized job/candidate schema, not just validation. |
| Orchestration | **LangGraph for the apply pipeline only** | *Deviation 1.* See §4. |
| LLM access | **LiteLLM** behind our own `LLMRouter` | *Deviation 2.* One SDK for OpenAI/Anthropic/Gemini/Groq/xAI/Ollama; we own routing, caching, budgets. |
| Browser | Playwright (Python, async) + persistent contexts | As briefed. |
| Database | PostgreSQL 16 + pgvector | As briefed. Single source of truth including queue state. |
| Queue | **Postgres `SELECT … FOR UPDATE SKIP LOCKED` in Phase 2, Redis + arq from Phase 5** | *Deviation 3.* Don't run Redis before there is concurrency to manage. |
| Cache / pubsub | Redis | LLM response cache, embedding cache, SSE fan-out. |
| Storage | Local volume behind a `Storage` port; S3/MinIO in Docker later | Resumes and screenshots. Swap without touching callers. |
| Auth | **Single-user session + OS keyring for secrets in Phase 1** | *Deviation 4.* The brief describes one candidate. Schema is multi-tenant-ready (`candidate_id` everywhere); the auth system is not built until there is a second user. |
| Deployment | Docker Compose (api, worker, browser, web, postgres, redis) | As briefed. |

**Why these deviations.** LangGraph everywhere adds ceremony to code that is a plain async
pipeline — in the research corpus the projects using it had 2–3 node graphs. Its real value is
checkpointing and `interrupt()`, which is exactly what human-in-the-loop application filling
needs and nothing else does. Redis and Supabase Auth are both infrastructure carrying no weight
in Phase 1; the ports are defined so they drop in later without rework.

---

## 3. System diagram

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Next.js dashboard                                                       │
│  Overview · Job Feed · Queue · Live Agent · Resumes · Companies ·         │
│  Outreach · Tracker(Kanban) · Analytics · Settings                        │
└───────────┬──────────────────────────────────┬───────────────────────────┘
            │ REST (commands, reads)           │ SSE  /events/stream
┌───────────▼──────────────────────────────────▼───────────────────────────┐
│  FastAPI                                                                 │
│  routers/ · auth · rate limits · request validation                      │
│  ─ NL Command Parser  (text → SearchConfig, the ONLY free-text entry)    │
└───────────┬──────────────────────────────────────────────────────────────┘
            │ enqueue(run)                              ▲ emit(event)
┌───────────▼───────────────────────────────┐  ┌────────┴─────────────────┐
│  DISCOVERY + INTELLIGENCE  (worker pool)  │  │  Event bus               │
│                                           │  │  append-only agent_event │
│  ATSFeedSource ─┐                         │  │  → Redis pubsub → SSE    │
│  CareersCrawler ├─► Normalizer ─► Dedup ──┤  └──────────────────────────┘
│  PortalSource*  ┘        │         │      │
│                          ▼         ▼      │
│              JobExtraction    JobIdentity │
│                    │                      │
│         ┌──────────▼───────────┐          │
│         │ Scoring (3 stages)   │          │
│         │ 1 deterministic gate │          │
│         │ 2 embeddings         │          │
│         │ 3 LLM (survivors)    │          │
│         └──────────┬───────────┘          │
│    CompanyIntel ───┤                      │
│                    ▼                      │
│            OpportunityRank                │
│                    ▼                      │
│           ApplicationDecision             │
└────────────────────┬──────────────────────┘
                     │ decision = APPLY → application row (status: SHORTLISTED)
┌────────────────────▼──────────────────────────────────────────────────────┐
│  APPLICATION EXECUTION   (LangGraph, checkpointed, one job per run)        │
│                                                                           │
│  tailor_resume → validate_evidence → detect_ats → extract_form            │
│      → map_fields → answer_questions → fill → verify_fill                 │
│      → presubmit_verdict → [interrupt: HUMAN] → submit → verify_submit    │
│                                                                           │
│  ATS adapters: Greenhouse · Lever · Ashby · Workday · Generic             │
│  Browser: Playwright, persistent context, trace + screenshot per step     │
└────────────────────┬──────────────────────────────────────────────────────┘
                     ▼
        Tracker → Analytics → Learning (ranking weights only)

* PortalSource adapters ship disabled. See §10.
```

---

## 4. Agent architecture

The brief's 18 agents are the right decomposition, but they are not 18 of the same thing. Three
distinct kinds, and conflating them is what makes these systems unmaintainable:

**Pure functions** (no LLM, no I/O, fully unit-testable — the reliability backbone)
`Normalizer` · `JobIdentity/Dedup` · `DeterministicScorer` · `OpportunityRanker` ·
`ApplicationDecision` · `PresubmitVerdict` · `EvidenceValidator` · `ATSValidator`

**I/O services** (network, retries, caching — integration-tested against fixtures)
`ATSFeedSource` · `CareersCrawler` · `CompanyResearch` · `Storage` · `Notifier` · `LLMRouter`

**LLM agents** (prompted, non-deterministic — always constrained by a schema and a validator)
`JobExtraction` · `SemanticMatcher` · `MatchExplainer` · `ResumeTailor` · `CoverLetter` ·
`QuestionAnswerer` · `IntentParser` · `FormFieldMapper`

The rule that follows: **no LLM agent's output ever reaches an irreversible action without a
pure function between them.** Tailored resume → `EvidenceValidator`. Answers →
`FactBank.supports()`. Field mapping → `PresubmitVerdict`. This is the architectural expression
of the brief's no-fabrication requirement.

### Why LangGraph only here

The apply pipeline needs: durable state across a human approval that may take hours; resume from
the exact node that failed; and a real `interrupt()` rather than a polling loop. That is
LangGraph's actual value proposition. Discovery is a `asyncio.gather` over sources with a
semaphore — expressing it as a graph adds nothing.

> **As built, second pass: LangGraph *is* used — for the campaign, not for the reason
> this section gives.**
>
> The first implementation skipped it, on the reasoning below. That reasoning was right
> about the apply pipeline and wrong about the system: the part that genuinely needs a
> graph is **discovery**, where sourcing is a map over (family × region × source) and
> enrichment is a map over companies. Both fan out to dozens of concurrent branches that
> all write into one state, and `Send()` plus state reducers make that merge the
> framework's problem instead of a hand-written one that has to be correct every time.
>
> The apply pipeline is a graph too, but for `interrupt()` rather than for parallelism:
> an approval can sit for days, and a checkpointer is what lets the run resume at the
> submit step instead of re-tailoring the resume and re-opening the form.
>
> The original objection still stands and is still load-bearing:
>
> The approval interrupt does not need durable *state* so much as a durable *browser page*.
> An approval is only meaningful if it approves the form the user actually looked at, and a
> page cannot be serialized into a checkpointer — so the hard part of the problem sits
> outside what a graph solves. What the pipeline needs is a live-session registry with a
> TTL, an eviction policy, and a fallback that re-prepares and says so when a session is
> gone. That is `applications/sessions.py`: 150 lines, and the whole of the interrupt
> semantics.
>
> The rest of the pipeline is a linear async function with explicit state transitions
> validated by a pure table (`applications/states.py`). Expressed as a graph it would have
> been the same sequence with a dependency and a second mental model on top. The rule above
> — no LLM output reaches an irreversible action without a pure function between them — is
> what actually needed enforcing, and it is enforced by module boundaries rather than by
> graph topology.

---

## 4b. The agentic layer

Added in Phase 3. Three layers, and the ordering is a dependency chain: prompts are text,
tools are capability, the runtime joins them to a model, and the supervisor uses all three.

```
  goal ──▶ ┌───────────────────────────────────────────────┐
           │  graph/supervisor.py — dynamic routing         │
           │  assess → decide ⇄ {discover, evaluate,        │
           │           research, queue, prepare} → report   │
           └───────────────┬───────────────────────────────┘
                           │ decide asks a model
           ┌───────────────▼───────────────────────────────┐
           │  llm/toolloop.py — the agent runtime           │
           │  turn limits · call limits · per-turn budget    │
           │  model fallthrough · tool errors fed back      │
           └───────────────┬───────────────────────────────┘
                           │ offers a named toolset
           ┌───────────────▼───────────────────────────────┐
           │  tools/ — 11 tools, effects declared           │
           │  read 9 · write 2 · external 0                 │
           └───────────────────────────────────────────────┘
                           ▲
           ┌───────────────┴───────────────────────────────┐
           │  prompts/ — 7 prompts, one copy of the rules   │
           └───────────────────────────────────────────────┘
```

**The capability boundary is the registry, not the prompt.** A prompt is a request a model
may decline to honour; a missing tool is a wall. So the properties that must hold are
expressed as absences: there is no tool that submits an application, sends a message, writes
to the Fact Bank, authenticates, solves a challenge, runs a command or fetches an arbitrary
URL. `tests/test_tools.py` asserts twenty-two such names do not exist.

Two of those absences carry most of the weight:

- **Nothing writes to the Fact Bank.** Every anti-fabrication check compares a claim against
  it. An agent that could add a fact could manufacture the evidence its own output is
  checked against, and every validator downstream would agree with it.
- **Nothing submits.** Submission lives behind the apply graph's `interrupt()`. `approved` is
  a parameter threaded from a human click, never ambient state, and no configuration turns
  it on globally.

**Toolsets are permission boundaries.** Each agent gets a named subset, chosen for what it
must not reach as much as for what it needs: the resume agent cannot look up a company (that
door is how unevidenced company detail reaches a resume), the ranking agent is read-only
because a ranking pass covers hundreds of jobs, and the learning agent can record a finding
but never act on one.

**Every limit that costs money is code, not instruction.** Turn and tool-call caps, a budget
re-checked every turn rather than once at the start, a step ceiling enforced by the router,
and a stuck-detector that ends a run when the same step is chosen with the same input three
times.

**Degradation is a working state.** With no model configured, a rate limit, or an exhausted
budget, the supervisor falls back to the fixed pipeline it replaced — discover, evaluate,
queue, report. It finds fewer of the right things and it says so; it does not refuse to run.
The fallback path is what the test suite exercises, because it is the path that runs on a bad
day.

**Prompts are versioned by content.** `tailor_resume@1.0+e6aeb4` is the declared version plus
a hash of the text, recorded on every decision a model made. Editing a prompt without
bumping its version still changes the recorded version, so two decisions made under different
instructions cannot look identical in the trail. The anti-fabrication rules exist once, in
`prompts.GROUNDING`, and compose into every prompt that touches candidate or company facts —
a prompt may opt out, visibly and with a failing test, but cannot opt into a softened variant.

## 5. Database schema

PostgreSQL. Abbreviated to columns that carry design decisions; timestamps and audit columns
omitted for readability.

```sql
-- ─── CANDIDATE KNOWLEDGE BASE ──────────────────────────────────────────────
candidate(id, name, email, phone, location, work_auth jsonb,
          salary_prefs jsonb, notice_period, availability)

-- The Fact Bank. Every claim the system may make, with its provenance.
-- Nothing reaches a resume or an answer unless it resolves to a row here.
candidate_fact(
  id, candidate_id,
  kind,              -- skill | project | experience | education | certification
                     -- | achievement | link | preference
  label,             -- 'PyTorch', 'Whisper fine-tuning', 'ML Intern @ X'
  body text,
  evidence_source,   -- master_resume | user_entered | project_readme | certificate
  evidence_locator,  -- page/line/URL — must be resolvable back to the artifact
  first_used_at date, last_used_at date,   -- for truthful "years of experience"
  strength smallint, -- 1-5, user-set; never LLM-set
  embedding vector(1024)
)
candidate_gap(id, candidate_id, skill, seen_in_job_count, first_seen_at)

-- ─── RESUMES ───────────────────────────────────────────────────────────────
resume_master(id, candidate_id, storage_key, parsed jsonb, is_active)
resume_variant(                        -- AI Engineer / ML / Data Analyst / …
  id, candidate_id, archetype, name, storage_key, source_master_id)
resume_render(                         -- one per tailored application
  id, variant_id, job_id, storage_key, content jsonb,
  fact_ids uuid[],                     -- exactly which facts were used
  ats_score numeric, evidence_integrity numeric, keyword_match numeric,
  validation jsonb)                    -- full validator output, not just a score

-- ─── COMPANIES + ATS REGISTRY ──────────────────────────────────────────────
company(id, name, normalized_name, domain, website, industry, size_band,
        hq_location, description, tech_focus text[], ai_relevance smallint)
company_intel(id, company_id, summary, signals jsonb, fit_score numeric,
              researched_at, expires_at)

-- The asset that makes discovery work. Built once per company, reused forever.
ats_board(
  id, company_id,
  provider,            -- greenhouse | lever | ashby | workday | …
  slug,                -- for workday: 'host::SiteName'
  discovered_via,      -- domain_guess | careers_page | widget_attr | manual
  confidence numeric,
  last_ok_at, last_job_count int, consecutive_failures int,
  UNIQUE(provider, slug))

-- ─── JOBS ──────────────────────────────────────────────────────────────────
job(                                   -- the deduplicated logical opening
  id, company_id, title, normalized_title, location, work_mode,
  employment_type, salary jsonb, description text,
  requirements jsonb, preferred_skills jsonb, education_requirements jsonb,
  experience_requirements jsonb, seniority, domain,
  posted_date, first_seen_at, last_seen_at,
  status,                              -- open | expired | filled | unknown
  fingerprint text,                    -- company+normalized_title+location hash
  embedding vector(1024))

job_listing(                           -- one row per place we saw it
  id, job_id, source, source_job_id, url, application_url, ats,
  scraped_at, raw jsonb,
  UNIQUE(source, source_job_id))
-- LinkedIn + Indeed + company ATS = 3 job_listing rows, 1 job row.

job_analysis(id, job_id, extracted jsonb, hidden_constraints jsonb,
             sponsorship_required boolean, keywords text[], model, cost_usd)

job_score(
  id, job_id, candidate_id,
  deterministic jsonb,   -- {skills, experience, education, location, …}
  semantic jsonb,        -- cosine sims per dimension
  llm jsonb,             -- {strengths, weaknesses, missing, strategy}
  job_fit numeric, company_fit numeric, final_score numeric,
  priority,              -- VERY_HIGH | HIGH | REVIEW | LOW | REJECT
  weights_version int)   -- which weight set produced this; needed for learning

-- ─── APPLICATIONS ──────────────────────────────────────────────────────────
application(
  id, job_id, candidate_id, listing_id,   -- which listing we applied through
  strategy,              -- PORTAL | ATS_DIRECT | CAREERS_PAGE | APPROVAL | OUTREACH
  resume_render_id, cover_letter_id,
  status,                -- see state machine §8
  ats, application_url, external_reference,
  submitted_at, evidence jsonb,           -- confirmation text/url/screenshot keys
  match_score, company_score, final_score,
  notes, follow_up_date,
  UNIQUE(job_id, candidate_id))           -- the duplicate guard, at the DB level

application_answer(
  id, application_id, canonical_field, question_text, answer text,
  fact_ids uuid[], confidence numeric, source)  -- generated | memory | human

question_memory(
  id, candidate_id, canonical_field, question_norm, answer,
  times_used int, last_used_at, requires_recheck boolean)

application_event(id, application_id, type, payload jsonb, at)  -- append-only

-- ─── OUTREACH ──────────────────────────────────────────────────────────────
outreach(id, candidate_id, company_id, channel, contact_ref, subject, body,
         resume_render_id, status, approved_by, approved_at, sent_at)
outreach_rate_limit(candidate_id, window_start, sent_count)

-- ─── AGENT OBSERVABILITY ───────────────────────────────────────────────────
agent_run(id, kind, trigger, search_config jsonb, status, started_at,
          ended_at, stats jsonb, cost_usd)
agent_event(id, run_id, type, job_id, application_id, level, message,
            payload jsonb, at)          -- powers the live feed; append-only
llm_call(id, run_id, agent, provider, model, prompt_tokens, completion_tokens,
         cost_usd, cache_hit boolean, latency_ms)
browser_action(id, application_id, step, action, selector, canonical_field,
               result, screenshot_key, at)

-- ─── CONFIG + LEARNING ─────────────────────────────────────────────────────
search_config(id, candidate_id, name, roles text[], locations text[],
              work_modes text[], employment_types text[], experience_band,
              salary_min, keywords_include text[], keywords_exclude text[],
              companies_preferred text[], companies_blocked text[],
              max_job_age_days int, sources text[], active boolean)
ranking_weights(id, candidate_id, version int, weights jsonb, active boolean,
                derived_from_run_id, rationale text)
autonomy(candidate_id, level smallint, auto_submit_threshold numeric,
         daily_application_cap int, requires_approval_for text[])
```

**Three schema decisions worth defending.**

`job` / `job_listing` split: the brief's §23 wants one job with multiple source URLs. Modelling
listings as rows rather than an array makes "was this posting removed?" and "which listing did we
apply through?" answerable, and makes the dedup key a database constraint rather than a
convention.

`resume_render.fact_ids`: the evidence link is stored, not recomputed. When a recruiter asks
about a bullet, the exact facts behind it are retrievable. It also makes the validator's job
mechanical — every generated claim must map to a `fact_ids` member.

`job_score.weights_version`: without it, the learning engine cannot distinguish "this job scored
badly" from "our weights were different then", and outcome analysis is meaningless.

---

## 6. API design

REST over `/api/v1`, JSON, Pydantic-validated. Reads are cheap; anything that costs money or
touches the outside world is a `run`.

```
# Candidate + facts
GET/PUT  /candidate
GET/POST /candidate/facts                 POST body includes evidence_locator
DELETE   /candidate/facts/{id}
GET      /candidate/gaps

# Resumes
POST     /resumes/master                  multipart → parse → fact extraction (review required)
GET      /resumes/variants
POST     /resumes/variants/{a}/render     {job_id} → tailored render + validation report
GET      /resumes/renders/{id}/download

# Search + discovery
GET/POST /search-configs
POST     /commands                        {text} → parsed SearchConfig preview (no side effects)
POST     /runs/discovery                  {search_config_id} → run_id
GET      /runs/{id}                       status + funnel counts

# Jobs
GET      /jobs                            filters: score, source, ats, age, status, priority
GET      /jobs/{id}                       job + listings + analysis + score + company
POST     /jobs/{id}/analyze
POST     /jobs/{id}/reject                {reason} → feeds learning
GET      /jobs/{id}/duplicates

# Companies
GET      /companies/{id}
POST     /companies/{id}/research
POST     /companies/{id}/discover-ats     → ats_board row

# Applications
POST     /applications                    {job_id, strategy} → SHORTLISTED
GET      /applications                    kanban view by status
POST     /applications/{id}/prepare       → runs the LangGraph up to the approval interrupt
GET      /applications/{id}/preview       filled fields, answers, resume, presubmit verdict
POST     /applications/{id}/approve       {field_overrides?} → resumes the graph
POST     /applications/{id}/reject
POST     /applications/{id}/takeover      → hands the live browser to the user
POST     /applications/{id}/status        manual: INTERVIEW | REJECTED | OFFER
GET      /applications/{id}/events

# Outreach  (no endpoint sends without an explicit approve)
GET/POST /outreach
POST     /outreach/{id}/approve
POST     /outreach/{id}/send

# Live + analytics
GET      /events/stream                   SSE: agent_event tail, filterable by run_id
GET      /analytics/funnel|by-dimension|resume-performance|costs
GET/PUT  /settings/llm|autonomy|weights
```

**`POST /commands` returns a preview, never an action.** The natural-language interface
(brief §26) parses to a `SearchConfig` the user can see and edit before anything runs. An LLM
misreading "apply to the top 10" must not be able to submit ten applications.

> **As built.** The surface above is implemented, with these differences:
>
> * Paths are `/resumes/tailor` (rather than `/resumes/variants/{a}/render`) and
>   `/applications/{id}/end-session`; `/applications/batch/prepare` prepares several at
>   once and submits none of them.
> * `POST /applications/{id}/approve` requires `confirm: true` in the body, and
>   `/outreach/{id}/send` and `/learning/proposals/{id}/apply` require `confirm=true` —
>   an accidental POST must not submit an application, send a message, or change how jobs
>   are ranked.
> * `GET /settings/llm` reports provider *reachability* and never returns or accepts a key.
> * `PUT /settings/autonomy` applies to the running process only. Persisting it would mean
>   a database value silently overriding the environment for the setting that governs
>   whether the agent submits unattended.
> * Routers live one-per-domain under `careeragent/routes/`; `api.py` keeps status,
>   candidate, jobs, events and discovery runs.

---

## 7. Browser agent design

### The canonical field catalog

The abstraction that makes adapters small (adopted from neonwatty, MIT). One vocabulary between
the candidate KB and every ATS:

```
contact.first_name  contact.last_name  contact.email  contact.phone
contact.location    resume.file        cover_letter.file
profile.linkedin    profile.github     profile.portfolio
employment.current_company             experience.years_total
authorization.work_authorized          authorization.sponsorship
demographic.*       compensation.expected          availability.start_date
question.freeform:<hash-of-question>
```

Each canonical field is resolved exactly twice: once from the candidate KB (`FieldResolver`,
pure, no LLM), and once per ATS to a selector (`adapter.extract()`). Unknown fields become
`question.freeform:*` and route to the `QuestionAnswerer`, which must ground every answer in the
Fact Bank or return `REQUIRES_USER_INPUT`.

### Adapter interface

```python
class ATSAdapter(Protocol):
    name: str
    def detect(self, url: str, page: Page) -> DetectionResult: ...
    async def extract(self, page: Page) -> FormSpec:        # → canonical fields + selectors
    async def fill(self, page: Page, plan: FillPlan) -> list[FillRecord]:
    async def verify(self, page: Page) -> list[FieldState]: # gather ONLY; no decisions
    async def submit(self, page: Page) -> SubmitAttempt:
    async def recover(self, page: Page, err: StepError) -> RecoveryAction:
```

`verify()` gathers raw DOM state and returns it. The decision lives in a pure function outside
the adapter:

```python
def presubmit_verdict(states: list[FieldState]) -> list[Failure]:
    """Empty list == safe to submit. No page access, no I/O, fully unit-testable."""
```

This split (reimplemented from Malik1942's pattern — see `00-research.md` §6) is what makes
submission safety testable without a browser. It catches the cases that actually bite:
`<option disabled selected>` placeholders, sentinel option values, a combobox whose readback
equals its own placeholder attribute, and `aria-invalid`.

### Execution rules

1. **Verify every write.** After filling a combobox or select, read it back; if the committed
   value is unrelated to the intended one, raise rather than continue.
2. **Never fill** demographics/EEO, veteran, disability, sponsorship declarations, or access
   codes. These go to the human, at every autonomy level. Answering them by inference is both
   wrong and offensive.
3. **Evidence or it did not happen.** `APPLIED` requires a post-submit confirmation signal
   (URL change, confirmation text, or a confirmation email match). Anything weaker is
   `SUBMISSION_UNVERIFIED`.
4. **Screenshot and trace every step**, keyed to `browser_action`. Screenshots are the evidence
   the user reviews and the debugging surface when an adapter breaks.
5. **One persistent browser context per portal**, stored encrypted. Sessions are user-created
   during a takeover; the agent never handles passwords.

### Testing (the corpus's best idea, adopted wholesale)

```
record (real form, once)  →  scrub PII  →  compile to fixture.json  →  replay in CI
                                                                          │
                                              oracle: finalActionActivations == 0
```

Every adapter ships with recorded fixtures. CI replays them offline and asserts the submit
control was never activated. A fixture carries `provenance` (recorder version, capture month,
source SHA-256) so a stale capture is visible rather than silently wrong. This is the only way
to test browser automation without hammering live job boards.

---

## 8. Application state machine

Transitions are explicit; anything else is a bug. The brief's status list, made executable:

```
DISCOVERED → SHORTLISTED → RESUME_TAILORED → READY
                                              │
                        ┌─────────────────────┼──────────────────┐
                        ▼                     ▼                  ▼
                AWAITING_APPROVAL       HUMAN_REQUIRED         REJECTED
                   │        │            (captcha/MFA/         (by user
              approve    reject           unknown field)        or decision)
                   ▼                         │
                APPLYING ◄───────────────────┘ (after takeover)
                   │
      ┌────────────┼────────────────┐
      ▼            ▼                ▼
  APPLIED   SUBMISSION_        FAILED ──► retry (bounded) or FAILED_TERMINAL
 (evidence)  UNVERIFIED
      │
      ├──► FOLLOW_UP_DUE ──► INTERVIEW ──► OFFER
      └──► REJECTED_BY_COMPANY              WITHDRAWN
```

`SUBMISSION_UNVERIFIED` is a first-class state, never collapsed into `APPLIED`. It surfaces in
the dashboard as an explicit "please confirm this went through" item, because the alternative —
quietly counting it as applied — corrupts every downstream analytic and, worse, may cause a
duplicate application later.

---

## 9. Human-in-the-loop

| Level | Grants | Default gates |
|---|---|---|
| 0 | Search only | — |
| 1 | + rank, score, company research | — |
| 2 | + tailor resumes, draft answers | Evidence validation must pass |
| 3 | + open browser, fill forms, stop before submit | Never submits |
| 4 | + queue for approval, submit on explicit approve | Per-application approval |
| 5 | + auto-submit above `auto_submit_threshold` | Only for adapters with green fixture status, known form shapes, score above threshold, under the daily cap |

**Default is level 4.** Level 5 never applies to: an ATS adapter without passing fixtures; a form
containing an unrecognized required field; anything with a demographic or sponsorship question;
any outreach, ever; or any application where `presubmit_verdict` returned a non-empty list.

**Hard stops — always human, at every level.** CAPTCHA, MFA, identity verification, any anti-bot
challenge, login screens, payment or ID-document requests. The agent pauses, emits
`HUMAN_APPROVAL_REQUIRED`, streams the live browser view, and hands over control. There is no
configuration that bypasses these, because bypassing them is exactly what the brief's §17 and §38
forbid.

---

## 10. Portal adapters and platform rules

LinkedIn, Indeed, Glassdoor and Naukri all prohibit automated access and scraping in their user
agreements. This is not a technical obstacle to route around — it is a product constraint, and
the brief's §38 already states the right principle ("design so adapters can be disabled if a
platform does not allow the intended automation"). Applied literally:

- **Portal adapters are a separate, optional package**, disabled by default, each behind its own
  feature flag with the platform's stance recorded in code next to the flag.
- **The MVP does not depend on them.** Discovery is built on ATS feeds and careers pages, which
  are published for exactly this purpose and carry no such prohibition.
- **Where a portal is used, prefer the human-assisted path**: the agent surfaces the listing and
  the user drives their own logged-in session, with the agent filling only after the user opens
  the form. This is genuinely useful and does not require the agent to hold portal credentials.
- **Where a job appears on both a portal and the company's own ATS** — which the research shows
  is common — the application strategy engine routes to the ATS. Better for the candidate
  (direct applications convert better), and it sidesteps the issue entirely.

This is a real constraint on the brief's §5 source list, and it is stated plainly rather than
designed around. The ATS-first architecture is not a workaround; on the evidence in
`00-research.md` §3 it is the better discovery channel regardless.

---

## 11. Security model

| Asset | Protection |
|---|---|
| LLM API keys | OS keyring (Phase 1) → age/SOPS-encrypted secrets or Vault. Never in DB plaintext, never returned by any API, never in the frontend bundle. A `GET /settings/llm` returns `{provider, has_key: true, last4}`. |
| Browser session cookies | Encrypted at rest (AES-GCM, key from keyring). Per-portal, per-profile. Never logged. |
| Portal passwords | **Not stored.** Sessions are established by the user during a takeover; the agent inherits the context. |
| Resume / PII | Object storage with no public URLs; signed, short-lived download links. PII scrubbed from any recorded fixture before commit. |
| Logs | Structured; a redaction filter strips email, phone, address, and anything matching a key pattern. Screenshots of filled forms are treated as PII and stored alongside resumes, not in logs. |
| LLM egress | Fact Bank entries sent to providers are the minimum needed for the task. Local Ollama routing is available for resume processing when the user prefers no third-party egress. |
| Outreach | Rate-limited per window, approval-gated, and the contact list is never bulk-exported. |

Threat model note: the realistic risks here are not attackers — they are *the system itself*
mis-sending the user's personal data, and secrets leaking into logs or the frontend. Both are
addressed structurally above rather than by policy.

---

## 12. Cost control

The funnel is the cost control. Per the research, LLM spend is governed by how few jobs reach
stage 3, not by prompt tuning.

```
5,000 discovered   → deterministic scorer (free)        → ~600 survive gate ≥0.35
  600              → embeddings (cached, ~$0.0001 ea)   → ~150 survive
  150              → LLM batch scoring (10/call, cheap) → ~35 strong
   35              → company research (cached 30d)      → ~18 worth applying
   18              → tailoring + answers (strong model) → 12 submitted
```

Routing: extraction/classification/dedup → cheap fast models (Groq, Gemini Flash, local).
Tailoring/company reasoning/complex answers → strong models. Private resume processing → Ollama
when configured. Every `llm_call` records tokens and cost, so the dashboard can show real
per-application spend rather than an estimate.
