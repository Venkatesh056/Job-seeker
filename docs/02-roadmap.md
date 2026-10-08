# Development Plan, MVP Scope and Risks

Companion to `00-research.md` and `01-architecture.md`.

---

## 1. MVP scope

The brief's §51 asks for a strong MVP rather than 30 portals. Taking the research into account,
the MVP is narrower than the brief suggests in sources and wider in safety machinery — because
the research shows sources are the easy part and safety is where these projects fail.

### In scope

```
Discovery
├── ATS feeds: Greenhouse, Lever, Ashby, Workday          (all verified live)
├── Careers-page ATS discovery (domain → provider + slug)
└── Company/ATS registry, built up as it runs

Intelligence
├── JD extraction → normalized schema
├── Deterministic scoring (gate)
├── Embedding similarity (pgvector)
├── LLM scoring + explanation on survivors
└── Duplicate detection across listings

Resume
├── Master upload → parse → Fact Bank (human-reviewed)
├── 3 archetype variants (AI Engineer, ML Engineer, Data Analyst)
├── Job-specific tailoring with fact_ids recorded
└── Evidence validation + ATS validation

Application
├── Adapters: Greenhouse, Lever, Ashby  (+ Generic, detect-only)
├── Canonical field catalog + resolver
├── Fill → verify-readback → presubmit_verdict
├── Approval queue + live browser view + takeover
└── Tracker with evidence-backed APPLIED / SUBMISSION_UNVERIFIED

Dashboard
└── Overview · Job Feed · Approval Queue · Live Agent · Resume Center · Tracker
```

### Explicitly out of MVP

Portal adapters (LinkedIn/Indeed/Naukri/Internshala/Glassdoor) — see §4 Risks. Outreach.
Analytics beyond a funnel count. The learning engine. Multi-user auth. Workday *submission*
(discovery only — Workday requires account creation per tenant, which is a human step).

**As built:** outreach, full analytics and the learning engine were completed after the
MVP and are in the shipped system (§2). Portal adapters and multi-user auth were not
built, and Workday remains discovery-only for the reason immediately below.

**Why Workday is discovery-only in MVP:** its feed is excellent and verified (paginated POST,
20/page), but applying requires creating an account on each tenant, with email verification.
That is a human action by design, and automating account creation is out of bounds.

---

## 2. Phases

Each phase ends with something demonstrable and tested. All ten are built; what follows
records what each actually delivered, because the exit criteria are the useful part —
they are what a later change has to keep true.

| Phase | Deliverable | Exit criterion | State |
|---|---|---|---|
| **0. Foundations** | Repo, Docker Compose, migrations, structured logging, event bus + SSE, settings/secrets via keyring | `agent_event` rows stream to a page in the browser | ✅ |
| **1. Candidate KB** | Profile, master resume upload + parse, Fact Bank, gaps, search config | Every fact has a resolvable `evidence_locator` | ✅ |
| **2. Discovery** | 14 ATS feed readers, careers-page discovery, `ats_board` registry, normalizer, dedup | 50 company domains → deduplicated jobs with descriptions | ✅ verified live |
| **3. Intelligence** | JD extraction, deterministic scorer, embeddings, LLM batch scoring, company research, ranking, decision engine | 1,000 jobs → ranked list with explanations under a stated budget | ✅ |
| **4. Resume intelligence** | 8 archetypes, tailoring with `fact_ids`, evidence validator, ATS validator, PDF render | Validator rejects a deliberately fabricated claim in a test | ✅ — and rejected a real one from Gemini on a live run |
| **5. Browser foundation** | Playwright runner, canonical catalog, field resolver, recorded fixtures, Greenhouse adapter | Greenhouse fixture replays green with `finalActionActivations == 0` | ✅ |
| **6. Apply pipeline** | Fill/verify/presubmit, approval queue, held live sessions, takeover, Lever + Ashby adapters | One application filled end to end with evidence, stopping for approval | ✅ |
| **7. Tracking** | State machine, duplicate guard, timeline, Kanban, follow-ups, manual status | Duplicate application impossible at the DB level | ✅ |
| **8. Analytics** | Funnel, response/interview/offer rates, breakdowns by six dimensions, cost per application | Per-application cost and outcome visible | ✅ |
| **9. Learning** | Outcome analysis → proposed `ranking_weights` version, diffed and applied only on approval | A weight change is proposed, explained, and reversible | ✅ |
| **10. Outreach** | Company targeting, contacts from permitted sources only, drafting, approval, rate limits, tracking | Nothing sends without an explicit approve; limits enforced in tests | ✅ |

### Deliberate departures from the original plan

Three things were built differently from the phase plan above, each for a reason worth
recording:

**LangGraph was not used.** The plan specified it for the apply pipeline, for durable
checkpointing across the approval interrupt. What the pipeline actually needs is one
browser page held open between prepare and approve — a live resource that no checkpointer
can serialize. A graph would have added a dependency and a mental model on top of a
registry with a TTL, without solving the part that is genuinely hard. `applications/sessions.py`
is that registry; it is 150 lines and it fails safe by re-preparing.

**The dashboard is one served HTML file, not Next.js.** Nine screens, a live SSE feed and
an approval flow, with no build step and no `node_modules`. Next.js remains the right
answer when several people maintain several screens; it is the wrong answer when the cost
is a toolchain you must keep working before you can look at a job listing.

**Portal adapters were not built at all**, rather than built and disabled. See §4.1.

---

## 3. Testing strategy

Mapped to the brief's §52, weighted by where the research says failures actually occur.

| Layer | What | How |
|---|---|---|
| Unit | `presubmit_verdict`, dedup fingerprinting, deterministic scorer, evidence validator, field resolver, normalizer | Pure functions, table-driven. These are the reliability backbone and must be fast and exhaustive. |
| Contract | Each ATS feed parser against captured API responses | Recorded JSON fixtures; a live health check (like ats-jobs' `live-health`) run on a schedule, not in CI |
| Browser | Each adapter's `detect/extract/fill/verify` | Recorded, PII-scrubbed form fixtures replayed offline; **tripwire asserts submit never activated** |
| Integration | Discovery run, state transitions, approval flow, LangGraph resume-after-interrupt | Test DB, no network |
| LLM | Extraction and answer generation | Golden-set with schema validation; assert grounding (every claim maps to a `fact_id`), not prose quality |
| Safety | Fabrication, never-fill, duplicate guard | Adversarial: feed a JD demanding a skill the candidate lacks; assert it lands in `candidate_gap`, not the resume |

---

## 4. Risks and limitations

Ordered by how likely they are to actually hurt this project.

### 4.1 Portal Terms of Service — the dominant risk

LinkedIn, Indeed, Glassdoor and Naukri prohibit automated access. Every repository in the
research corpus that automates them does so in breach of those terms, and none acknowledges it.
Consequences are real: account restriction or permanent ban on the account the user needs most
for their job search.

*Mitigation.* ATS-first architecture; portal adapters isolated, flagged and off by default;
human-driven sessions where a portal is used; ATS routing preferred when a job appears in both
places. **Accepted limitation:** the system will discover fewer jobs than a scraper would. On the
evidence, it will discover better-structured ones, and it will not get the user banned.

### 4.2 Fabrication risk

An LLM asked to make a resume fit a JD will invent. This is the risk most likely to cause
concrete harm to the user — a fabricated claim surviving to a recruiter is a career problem, not
a bug report.

*Mitigation.* Fact Bank with resolvable provenance; `fact_ids` recorded per render; a pure
validator between generation and output; gaps recorded rather than filled;
`REQUIRES_USER_INPUT` as a first-class answer. **Residual risk:** subtle overstatement
(implying depth of experience) is harder to catch than invented facts. Phase 4 should include a
second-pass verification prompt comparing each bullet against its cited facts.

### 4.3 Browser automation brittleness

ATS forms change without notice. Selectors rot. Expect ongoing maintenance, not a finished
adapter.

*Mitigation.* Canonical fields keep the breakage surface small; fixtures with `captureMonth` make
staleness visible; `recover()` plus a failed-application queue; per-adapter health status gating
autonomy level 5. **Accepted limitation:** adapters need periodic re-recording. Budget for it.

### 4.4 Slug resolution coverage

Per `00-research.md` §3, several platforms return `200 OK` with an empty list for a wrong slug.
A silent under-fetch looks exactly like "no openings".

*Mitigation.* `emptyMeansExists` per provider; careers-page discovery before trusting a guess;
`ats_board.consecutive_failures` and `last_job_count` to detect a board that has gone quiet;
alert rather than assume. This is the discovery equivalent of the silent-wrong problem, and it
deserves the same rigour.

### 4.5 Cost drift

Naive implementation runs an LLM over every discovered job. At 5,000 jobs that is real money.

*Mitigation.* The funnel (`01-architecture.md` §12), aggressive caching keyed on content hash,
batched scoring, cheap models for extraction, local models optional. Per-run budget caps that
degrade to keyword-only scoring rather than failing.

### 4.6 Learning-loop sparsity

The brief's §29 wants learning from outcomes. A single candidate generates maybe 50–200
applications with a handful of interviews. That is far too little data for statistical weight
optimization — naive fitting will overfit to noise.

*Mitigation.* Treat learning as *proposal*, not optimization: surface descriptive patterns
("AI Engineer applications: 11% response vs 3% for Data Analyst, n=28 and n=31"), propose a
weight change with its sample size stated, and require user approval. Never auto-apply.
**Accepted limitation:** with this data volume, the learning engine is a decision aid, not an
optimizer. Claiming otherwise would be dishonest about what the numbers can support.

### 4.7 Single-candidate design

The brief describes one candidate. The schema carries `candidate_id` throughout, but auth,
tenancy isolation, and per-user rate limits are not built. Retrofitting real multi-tenancy is a
meaningful piece of work if this ever serves other people.

### 4.8 Verification gaps

Not every ATS gives a reliable submission confirmation. `SUBMISSION_UNVERIFIED` exists precisely
because this cannot always be resolved automatically. Some applications will need the user to
confirm manually — by design, since the alternative is a system that lies about what it did.

---

## 5. Questions the build answered

These were open at design time. The implementation settled them; they are recorded here
because reopening any of them changes the system.

1. **Target geography and roles.** The Fact Bank was built against a real MSc Data
   Science resume. Eight archetypes exist, and `for_title()` picks one from the job title
   rather than requiring a choice up front — so the question stopped needing an answer.
2. **LLM budget.** A hard daily ceiling (`llm_daily_budget_usd`, default $2) that degrades
   to deterministic scoring rather than failing. Measured cost on a real run: **$0.0088**
   to tailor one resume on the strong tier, and $0 for the deterministic path. Discovery
   over 1,144 jobs cost nothing, because the funnel gated every one of them out before the
   LLM saw it.
3. **Autonomy comfort.** Default is level 4, approval-always. Level 5 exists and is
   deliberately hard to reach: an allow-listed ATS, an empty verdict, no unanswered
   required field, a score above threshold, and under the daily cap.
4. **Portal stance.** Not built. ATS-first discovery reached 1,144 jobs across 75 boards
   without them.

---

## 6. Next step

Phase 0 + 1: scaffold the repo, Docker Compose, migrations, event stream, and the candidate
knowledge base with Fact Bank ingestion from your master resume. That gives a working spine and
the piece everything else depends on — and it needs your actual resume to be worth anything.
