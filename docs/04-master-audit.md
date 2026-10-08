# Master Audit — 2026-09-14

Against the Master Intelligent Implementation & Audit Checklist.

Nothing below is marked ✅ because a file, class, route or button exists. Every ✅ was
verified by running it against real data in this session or by a test that fails when the
behaviour is removed. Where a percentage is given it is a judgement over the checklist's own
sub-items, not a feeling.

```
870 tests · 104 API operations · 24 tables · 115 source files · ruff + mypy clean
```

---

## Overall status

```
Overall:                            58%

Agentic Architecture:               60%
Company Discovery:                  35%
Job Discovery:                      55%
Career Page / Direct Application:   70%
Resume Intelligence:                90%
Browser Automation:                 75%
Outreach:                           25%
Knowledge Graph:                    65%
Dashboard:                          55%
Reliability:                        70%
Security:                           60%
Production Readiness:               35%
```

The shape of this is deliberate and worth reading before the detail. **Everything from
"understand the candidate" through "apply to one job" is strong.** Everything that needs a
paid external provider, a second user, or a third-party portal is weak or absent. The system
is a good single-user career agent and not yet a product.

---

## ✅ COMPLETE

Verified working against real data.

### §1 Generalization (partial — see below for the multi-user half)
- Not hard-coded for one candidate. The only occurrence of the test candidate's name in the
  codebase is a comment explaining a bug fix.
- 56 job families across technology, engineering, healthcare, business, manufacturing.
- Arbitrary resume, profession, industry, region, job category.

### §2 Resume ingestion
- PDF, DOCX, TXT, MD. Contact, education, experience, skills, projects, certifications,
  achievements, titles, technologies extracted. Original preserved. Parse failures surfaced.

### §3–4 Candidate knowledge base and Fact Bank
**The strongest part of the system.** Every fact stores the resume line it came from.
Verified live: a model claimed "2 years of experience" against facts supporting 0.2 and the
validator rejected the whole document; a form claiming 7 years was blocked pre-submit with
*"the form claims 7 years; your resume supports 0.2"*.

All ten "no fabricated X" hard rules hold, enforced by `resume/validate.py`,
`agents/verification.py` and `applications/answers.check_grounding`.

### §6 Region engine
City/state/country/remote/hybrid/multi-location, normalized, scored, no city hard-coding.

### §15–16 Career page and direct openings
Company → website → careers → ATS → openings. 14 ATS platforms: Greenhouse, Lever, Workday,
Ashby, SmartRecruiters, Workable, Recruitee, BambooHR, Breezy, Teamtailor, Personio,
Rippling, Keka, Freshteam. Canonical application URL, stale-opening detection.

### §18–19 Job normalization and source verification
One canonical job object. Content-fingerprint dedup across sources. Verified: a monitor sweep
over 8 boards found 260 openings, an immediate re-sweep found 0.

### §20–21 Job intelligence and matching
Deterministic + embeddings + LLM, three stages, blended. The LLM is not the only mechanism —
stage 1 carries the largest weight because it is the only one inspectable line by line.

### §25 Job-specific resume optimization
8 archetypes, exact JD used, skills and projects reordered, summary customised, ATS
validated, fact-verified, versioned, application linked to the exact version. Measured 4 of 4
real jobs accepted after the truncation fix.

### §27–28 ATS adapters and browser agent
Playwright. `detect / extract / fill / validate / submit / recover` per adapter. ~100 form
field types catalogued with what may be written into each. Verified end-to-end against a
local fixture: parked at approval, resumed in a **separate process**, submitted with
`confirmation_text: 'Thank you for applying'`.

### §30–31 Application questions and verification
Evidence-based answers with citations, answer memory, escalation on unknowns. Pre-submit
contradiction check can and does block submission.

### §33 Career Supervisor
Plans, delegates, calls tools, branches, loops, retries, recovers, stops, resumes after
restart. Verified live: chose `evaluate → queue → report` unprompted and recognised the goal
was already met.

### §35 LangGraph (partial — 3 of 7 graphs)
Genuinely used. Typed state, conditional routing, checkpointing (SQLite/Postgres), human
interrupts, parallel `Send()` fan-out, recovery. **Durability verified across two processes.**

### §58–59 Prompt security and agent permissions
Untrusted text neutralised (`agents/trust.py`). The tool layer is the permission boundary:
11 tools, 7 per-agent sets, **zero with external effect**. No tool submits, sends, edits the
Fact Bank, authenticates, solves a challenge, runs a command or fetches an arbitrary URL —
and `tests/test_tools.py` asserts 22 such names do not exist.

### §65 Checkpointing and recovery
A parked approval survives a process restart and resumes at the submit step rather than
re-tailoring.

### §75 No fake results
No fabricated companies, jobs, applications, submissions or analytics. Rates are withheld
below n=8 rather than shown as a percentage. `APPLIED` requires evidence;
`SUBMISSION_UNVERIFIED` is a first-class state.

---

## 🟡 PARTIAL

### §1 Multi-user — 🔴 within a ✅ section
**Current:** single user. No `user` table, no tenant column, no auth.
**Missing:** users, isolation, sessions.
**Required:** a `user` table, `user_id` on 12 tables, auth middleware, per-request scoping.
**Note:** this is the single largest structural gap. Every table would need migrating.

### §5 Career taxonomy
**Current:** `expand(['machine_learning'])` → `machine_learning, ai_engineering, data_science,
mlops, deep_learning, computer_vision`. Works, stored, drives search.
**Missing:** the checklist's "no fixed career list required". It **is** a fixed list of 56
families. A career outside them expands to nothing. No LLM-driven expansion of an arbitrary
concept, no accept/reject UI for suggested roles.

### §7 Industry engine
**Current:** families carry a `group` (Technology, Engineering, Healthcare, Business…), and
company research records `tech_focus`.
**Missing:** industry as a first-class filter, custom industries, industry-driven company
discovery or ranking.

### §8 Company discovery
**Current:** careers-page and ATS resolution from a company name; domain guessing; 6 job
aggregators supplying company leads.
**Missing:** region-based, industry-based and profile-based *company* discovery. No MNC /
product / service / startup / manufacturing segmentation. Companies are found because they
posted a job, not because they are the right kind of company.

### §13 Multi-source company fusion
**Current:** name normalization, domain matching, duplicate prevention by fingerprint.
**Missing:** entity resolution across providers, provider ID mapping, retained raw provider
results. Fixed this session: board slugs were being used as company names, so
`quantiphi.wd1.myworkdayjobs.com::Careers_at_Quantiphi` sat beside `Quantiphi` as a separate
employer. 93 polluted rows removed.

### §14 Company enrichment
**Current:** website, domain, description, tech focus, signals, fit score, careers page, ATS,
confidence — all from the company's own pages, robots-respecting.
**Missing:** size, company type, leadership, hiring signals, announcements, structured
provenance.

### §17 Job discovery sources
**Current:** 14 ATS ✅. 6 aggregators built (Adzuna, JSearch, Jooble, The Muse, Arbeitnow,
Remotive) — **3 need keys you do not have yet**, so regional coverage is thin: a Bangalore
search returns ~40 remote companies instead of hundreds of local ones.
**Missing:** LinkedIn, Indeed, Naukri, Internshala, Glassdoor, Wellfound. See BLOCKED.

### §23 Opportunity ranking
**Current:** all 8 components, configurable weights, priority levels, explanations,
reproducible.
**Missing (found during this audit):** the bands are 0.92 / 0.85 / 0.75 / 0.60 but the best
achievable score is **0.77**, because `company_fit` is 0 for unresearched companies and
stages 2–3 have not run for most jobs. **Nothing can currently reach "high" or "very high".**
Either recalibrate the thresholds or run research and semantic scoring over the shortlist.

### §26 Company-level application — **done**
**Built:** `applications/company_apply.py`, `GET /companies/{id}/openings`,
`POST /companies/{id}/apply`, and an "Openings" button on the Companies page.
Discover all openings (re-reads the company's ATS boards) → filter (hard fails, already
applied, closed, score floor) → rank → one application and one tailored resume per opening.

**Three things it deliberately will not do**, so this is not a mass-application button:
it plans before it acts (`/openings` changes nothing and shows every refusal with its
reason); the daily cap binds inside the loop and is reported when it holds anything back;
and nothing is submitted — every prepared application lands in the approval queue.

**Verified live:** Quantiphi, 41 openings, 6 newly discovered on the refresh, 1 applyable
at 0.75, 40 refused with differentiated reasons. Refusal path tested against a job id
belonging to another company. 20 tests.

**Found while verifying:** across all 64 companies only **2** have an applyable opening.
The refusals break down as 426 "posted more than 30 days ago", 163 weak score, 149
seniority, 11 employment type. The freshness limit, not the scorer, is what empties the
funnel — see the note under priorities below.

### §41 Contact discovery — **blocked by Apollo's plan, repurposed**
**Measured, not assumed:** Apollo's free plan returns 403 `API_INACCESSIBLE` for both
`mixed_people/search` and `mixed_companies/search`. Contact discovery is not available at
this tier, and no amount of code changes that.

**What the plan does allow turned out to be worth more:** `organizations/enrich` and
`organizations/{id}/job_postings`. Built as `sources/apollo.py`. Nothing in it requests,
stores or returns a person — a deliberate boundary, not an accident of the tier.

### Database safety — **rebuilt as a refusal, not a convention**
`storage/safety.py`. Two conditions must *both* hold before anything destructive runs:
`CAREERAGENT_ENV` is `test`, **and** the URL is disposable (in-memory, under a temp
directory, or named test). Either alone is reachable by accident — a stale `ENV=test` in a
shell pointed at the real file, or a temp path on a production host.

Development is deliberately not permitted: a developer's own database holds the corpus
they have been building for weeks, which is exactly what was lost. `force=True` exists for
`careeragent init --reset`, which asks first, and is still refused against a declared
production environment.

`tests/conftest.py` is the floor beneath the per-file fixtures: it pins `CAREERAGENT_ENV`
to test before any import, defaults the database URL to a temp file, strips every provider
key from the environment, and **fails any test that ends up pointed at a non-disposable
database**. The old design made safety a convention each file re-implemented — so a file
that had never needed one had nothing to forget.

`storage/backup.py` adds snapshot, restore, prune and health. Snapshots use SQLite's own
backup API rather than a file copy, so they are safe against concurrent writes; a restore
snapshots what it is about to replace first. Verified on the real database: integrity ok,
26 tables, 12.7 MB snapshot.

`tests/test_database_safety.py` reproduces the incident exactly — a production-shaped
database, the same `init_db(drop=True)` call — and asserts the refusal, the surviving
tables and the surviving rows. A repository-wide sweep asserts no test file drops a schema
without redirecting the URL first.

### Unknown ≠ Zero — **fixed globally**
`models/evidence.py`. The same bug was found three times in this project: an unreadable
website scored 0.0 rather than "not assessed" (Swiggy sat at 0.00, then 0.87 once Apollo
filled the gap); 42 of 63 companies overlapped on no technology because a marketing page
says "we deliver value"; and 211 Apollo postings with no body text all matched zero skills.

Three states now — `KNOWN`, `UNKNOWN`, `NOT_APPLICABLE`. An unknown component **surrenders
its weight** instead of contributing a zero, and `NOT_APPLICABLE` does not depress coverage
at all, because a question that does not apply is not evidence we are missing.

Every score now carries `confidence`, `evidence_coverage` and `unknown_fields`, so a 0.9
built on one signal is distinguishable from a 0.9 built on six. `company.observe_fit()`
returns the full answer; `score_fit()` is the same code behind the two fields most callers
want, so they cannot drift.

### ⚠️ A test dropped the production database — cause removed

**What happened.** A test I added to exercise Apollo's credit-exhaustion path called
`init_db(drop=True)` without the fixture that redirects `CAREERAGENT_DATABASE_URL` at a
temp file. A routine `pytest` run therefore dropped every table in the real database,
destroying **1,756 jobs, 67 companies (63 researched), 81 ATS boards** and every score.

**Why nothing caught it.** Every other test file that drops tables defines an `isolated`
fixture or sets the environment variable inline. `test_apollo.py` had been pure-unit until
that test, so there was no fixture to forget — and the test's own code looked correct in
isolation. Absence is what a reviewer does not see.

**The fix is not "be careful".** `init_db(drop=True)` now **refuses** unless the URL looks
disposable — in-memory, or a file under a temp directory — and a caller that genuinely
means it passes `force=True`. The CLI's `init --reset` does, after asking. Verified against
the real URL: it raises rather than truncating. 7 tests cover the guard.

**Recovery.** The candidate profile was re-imported from the resume PDF (49 facts), the
preferences were restored, and the 66 seed domains are being re-resolved and swept. What
cannot be recovered now is the company research: Apollo's credits are spent for this
billing cycle, so `company_fit` will stay unknown until it resets. The web-only half of
research still works and can be re-run at any time.

### Apollo as a job source — **built, and it found 211 real openings**
`apollo.harvest_postings()` asks Apollo for openings at every company already held. Apollo
is keyed on a **domain**, not a board slug, so it reaches the two cases the board layer
cannot: companies whose ATS was never resolved, and companies whose careers page is a
JavaScript shell.

**Measured:** 67 companies asked, 362 postings seen, **211 new jobs, 108 of them within 30
days** — against a corpus that previously held only 457 fresh jobs in total.

**The catch, and the fix.** Apollo carries no description, and skill matching reads the
description. All 211 scored `reject`, the best at 0.33, including a one-day-old internship
in Bangalore — the "unknown scored as zero" mistake for the **third** time in this codebase.
`pipeline/describe.py` fetches the page for postings that could plausibly survive scoring
(fresh, right country, not senior, not already applied to) and rescores them. Of 192
description-less jobs only **27** were worth a request; all 27 read successfully, and the
scores moved in **both** directions — "GM/AVP Artificial Intelligence" 0.33 → 0.49, "IT
Procurement" 0.27 → 0.21 now that the scorer could see it genuinely does not match.

**Bug found by its own dry run:** 16 postings located in `"India"` were being refused a
read as "not somewhere you want to work". A country is *less specific* than a city inside
it, not somewhere else — and less specific is exactly what one request resolves.

### Apollo credits — **exhausted, and the status page was lying about it**
Apollo returns HTTP 422 for a spent account, the same status as a malformed request, with
`BILLING.LIMIT.CREDITS_EXHAUSTED` in the body. Without recognising that code, an exhausted
account is indistinguishable from a bad domain and a run keeps asking — which is what
happened: a harvest checked 67 companies, ran out partway, and the last six read as six
unrelated company failures.

Worse, the Keys page reported Apollo as **working**, because `auth/health` returns
`{"healthy": true}` for a key with zero credits. The probe now makes one real enrichment
call, reads Apollo's published rate-limit headers (600/day, 200/hour, 50/minute), and
reports `exhausted` with the reset explained.

**This session consumed your free Apollo credits** — roughly 133 calls across company
research and the posting harvest. They reset with the billing cycle.

### §23 Ranking ceiling — **fixed**
**The defect, measured:** `company_fit` was **0.0 on all 1,536 open jobs**. Three separate
faults, each of which alone would have produced the same symptom.

1. **Nothing filled it.** Company research reads a company's own site, and most large
   Indian employers serve a JavaScript shell — Swiggy's home page yields 57 characters.
   Apollo now supplements the read: gaps only, never overriding what a company says about
   itself, with the provenance recorded in `signals["apollo"]["filled"]`.
2. **`rescore` read the stored value**, so researching 63 companies moved nothing — every
   job kept the 0.0 it was first scored with. Now read fresh from `company_fit_map`.
3. **`rescore` never wrote it back.** Once the read was fixed the Jobs page showed
   `co=0.00` beside a score company fit had already moved — a number contradicting itself.

**Two more bugs found while verifying:**
- The uninformative-read branch assigned a *fresh* signals dict, erasing Apollo's record
  along with the content-derived signals — dropping the strongest fit component for exactly
  the companies whose sites are unreadable. Swiggy: 0.35 → **0.87**.
- `"Bengaluru"` did not match a preference of `"Bangalore"`, scoring a Bangalore company as
  being in the wrong place. Added a city alias map.

**Tech-overlap recalibration.** Across the 63 researched companies, **42 overlapped on
nothing** with a candidate holding 50 skills; mean overlap 0.63. A flat "5 overlaps for
full marks" made the heaviest component (weight 0.35) score **0.13 on average**, crushing
every company — the "unknown scored as zero" mistake in a different hat, because a
marketing page says "we deliver value", not "we use pandas". The denominator is now what
the company could plausibly have overlapped on, and below 8 known technologies an absence
is reported as "too few to judge" rather than scored. Mean fit **0.29 → 0.35**, bottom
bucket 15 → 6.

**Result:** 63 of 67 companies researched. `company_fit > 0` on **165 of 1,536** jobs (the
rest hard-fail before company fit is reached, which is correct). The Outreach page's "worth
contacting" list, previously empty, now ranks Swiggy and Meesho at 0.87.

**Still true:** the best job scores 0.71 against bands of 0.92/0.85/0.75/0.60, so nothing
reaches "medium". That is now a corpus problem, not a scorer problem — see priority 2.

### §31 Monitoring — job closing
**Also found:** 30 jobs carried `signzy::54e30b3d-…` as the employer, from the board-slug
bug fixed earlier in rows written before the fix. Repaired. Note the near-miss: the repair
script initially also "fixed" `Lemon.io` → `Lemon` and `A.Team` → `A`, which are the real
company names. Only `::` is a safe slug marker; a dot is not.

### §43 Gmail outreach — **built, awaiting your consent**
`outreach/gmail.py`, written against the REST API with `httpx` rather than Google's client
libraries — the two operations needed are a form POST and a JSON POST, and the scope stays
visible in one place instead of inferred from a library's defaults.

**The scope is `gmail.send` and nothing else.** It cannot read, search or delete anything
in the mailbox. A test asserts the scope list, because it is the one boundary in that file
that no amount of careful code below can enforce.

`applications.outreach.deliver()` applies every existing guard — approved status, a contact
with a permitted recorded source, the daily and weekly rate limits — then sends, then
records Gmail's message id as evidence. A failed send leaves the record **approved**, not
sent: marking it sent would lose the message and claim something that did not happen.
Migration `6ca1cb89e2c5` adds `transport`, `transport_message_id`, `transport_thread_id`.

Endpoints: `/outreach/gmail/status`, `/authorize`, `/callback`, `/disconnect`, and
`/{id}/deliver?confirm=true`. 23 tests. **Nothing has been sent** — consent is interactive
and yours alone.

### §12 Search provider layer — **done**
**Built:** `sources/search/` — `base.py` (what a result is and what may be believed about
it), `providers.py` (Tavily, SerpAPI), `__init__.py` (`search`, `find_companies`,
`harvest`). 53 tests.

Tavily is asked **first** despite SerpAPI returning better results, because the free tiers
are 1,000 credits and 250 searches respectively; "best provider first" would empty the
small budget in one run. An exhausted provider is not asked again for the rest of a run.
Every call is recorded via `integrations.record_call` and shows on the Keys page.

`harvest()` is the payoff: search → company domain → ATS discovery → a board that is then
monitored for free forever. The report counts **boards**, not leads, because a lead is a
hostname someone's SEO put in front of us and a board is a feed that keeps paying out.

**Measured yield, and it is poor.** 12 search calls across three role/city pairs produced
1 board. The searches return listicles and directories far above employer sites; after
adding bebee, f6s, themanifest, clutch and goodfirms to the block list and rejecting pages
whose *title* reads like a ranked list, the leads are clean but few. The real employers
that do surface — Microsoft, McAfee, Warner Bros Discovery — run custom career portals
with no discoverable ATS. At roughly 1 board per 12 credits, Tavily's 1,000/month buys
~80 boards a month. Useful, not transformative. **Adzuna would give more for less.**

**Bug found by its own test:** `skip = skip or set()` replaced a caller's *empty* set with
a throwaway copy, so the exhausted-provider skip never worked — not in tests and not in
the real `find_companies` path.

### §31 Monitoring — closing what has gone
**Fixed (found during this session):** no job in the database had ever been marked closed
— 1,528 rows, every one `status="open"`, including postings from 342 days ago. Two faults
stacked. The employer name was read from the raw ATS row, which Greenhouse leaves empty,
so the closing branch never ran on any board; and when it did run it only counted, never
writing the status. A year-old posting was therefore still offered as a live opening and
then refused for its age.

Now marked closed rather than deleted (an application sent against one is still real),
scoped to the employers a fetch actually returned (a generic slug like `rippling:careers`
carries several), never on an empty feed (indistinguishable from a broken board), and
reopened if a posting returns. A forced sweep of all 79 boards closed 9. 8 new tests.

### §29 Browser agentic loop
**Current:** goal → inspect → map → fill → verify → recover, deterministically, with a
~100-entry field catalogue and pure-function pre-checks. Not blind coordinate clicking.
**Missing:** an LLM reasoning loop for genuinely unknown forms. The current design handles
known shapes well and escalates the rest to you, which is safer but less capable.

### §37–39 Knowledge graph
**Current:** 10 node kinds, 11 edge kinds, projected on read so it cannot go stale. Four
named queries. Verified: 121 nodes / 241 edges in 43ms; click-to-recentre works.
**Missing:** Contact, Lead, LinkedIn Profile, Email, Outreach, Interview, Offer nodes — the
model defines them, but nothing produces that data because outreach does not run.

### §49 Dashboard
**Current:** 14 screens, live SSE feed, no build step. Dashboard, Companies, Jobs,
Applications, Resume Center, Outreach, Agent Activity, Graph, Analytics, Settings, Goal,
Watch, Looking for, Why.
**Missing:** Important People, Leads, Integrations. And it is **plain HTML, not Next.js /
React / TypeScript / Tailwind / shadcn** as specified.

### §55 Analytics
**Current:** funnel, response/interview/offer rates with sample sizes, breakdowns by role,
location, company, source, resume version, strategy.
**Missing:** breakdown by industry, ATS, outreach channel — no data for those yet.

### §57 Data provenance
**Current:** `discovered_via` on ATS boards; `agent_decision` records who decided what, why,
with what evidence and which prompt version.
**Missing:** systematic who/when/where/confidence/last-verified on companies, jobs, people.

### §60 Database
**Current:** 24 tables covering candidate, facts, resumes, versions, companies, jobs, scores,
applications, questions, answers, events, agent runs, LLM usage, decisions.
**Missing:** Users, company sources, job sources, leads, browser sessions, API integrations.
**And it is SQLite, not PostgreSQL + pgvector** — the code supports Postgres and
`EmbeddingType(384)` exists, but you are running SQLite.

### §67 Cost control
**Current:** staged intelligence (cheap → deterministic → semantic → expensive), token and
cost tracking per call, content-hash caching, daily budget ceiling, per-turn budget re-check
in agent loops.
**Missing:** Apify/Vibe/Clay/search usage tracking — those providers are absent.

### §68 Testing
**Current:** 870 tests. Unit coverage for parsing, facts, taxonomy, normalization, matching,
ranking, dedup, validation. Integration for Postgres-shaped storage, LangGraph, state
machine, approval flow. Browser tests against recorded fixtures.
**Missing:** integration tests for Apify, Vibe, Clay, search, Redis, MCP, Gmail, LinkedIn —
none of those exist to test.

---

## 🔴 MISSING

Not implemented. No file, no stub, no partial.

| § | Feature | Why it matters |
|---|---|---|
| 9 | **Apify integration** | The checklist's scalable discovery/crawl layer. No token management, actor registry, run tracking, dataset retrieval. |
| 10 | **Vibe Prospecting** | Contact and company search/enrichment. Nothing. |
| 11 | **Clay** | Enrichment and qualification layer. Nothing. |
| 12 | **Search provider layer** | No Tavily, SerpAPI, Brave or Google PSE. Company discovery has no web-search route. |
| 36 | **MCP** | No MCP server or client. The tool layer is internal-only. |
| 41 | **Contact discovery** | No Important Person agent, hiring-manager, recruiter, founder or engineering-leader discovery. `OutreachContact` is a table nobody fills. |
| 42 | **LinkedIn outreach** | Nothing. |
| 43 | **Gmail outreach** | No OAuth, no send, no thread tracking, no reply detection. |
| 44 | **Cross-channel timeline** | Nothing to cross. |
| 45 | **Outreach campaigns** | No campaign object, limits, or per-campaign tracking. |
| 48 | **Outreach compliance** | Rate limits and opt-out handling do not exist because sending does not exist. |
| 61 | **Redis / worker system** | Redis is in `requirements.txt` and unused. The pool is in-process asyncio. |
| 63 | **Next.js frontend** | Plain HTML. |
| 63 | **Supabase auth / storage** | Neither. Local filesystem, no auth. |
| 63 | **LangSmith** | No tracing. |
| 64 | **Provider abstraction interfaces** | `JobSource` and `LLMProvider` are abstracted. `CompanyEnrichmentProvider`, `ContactDiscoveryProvider`, `ContactEnrichmentProvider`, `SearchProvider`, `BrowserProvider`, `LinkedInProvider`, `GmailProvider` do not exist as interfaces. |
| 74 | **Failure-injection tests** | Individual failure paths are tested; there is no systematic simulation suite. |

---

## ⚠️ BLOCKED — external dependency

| Blocker | Effect | Who unblocks |
|---|---|---|
| **Adzuna, JSearch, Jooble keys** | Regional job coverage. Without them a Bangalore search finds ~40 remote companies, not hundreds of local ones. Code is written and tested. | You — free tiers |
| **LinkedIn / Indeed / Naukri / Internshala / Glassdoor** | Their terms prohibit automated access. Adapter slots and an `enable_portal_adapters = False` switch exist; nothing is behind them. My position: I will build these to work with **you logged in** in a visible browser with the agent taking over — not credential submission or bot-check evasion. | Your decision on the approach |
| **Apify / Vibe / Clay** | Paid accounts and tokens. | You |
| **Gmail OAuth** | Google Cloud project, consent screen, credentials. | You |
| **Gemini free-tier rate limit** | ~10 calls/minute. A supervisor run is 4–8 calls, so consecutive runs degrade to the fixed order. | A paid key or local Ollama |

---

## ❌ FAILED TESTS

No failing tests. 870 pass.

Failures **found and fixed** during this work, recorded because each was invisible:

1. **Skill matching was a substring check.** A description mentioning none of the candidate's
   skills scored **1.0 skill overlap** — "excellent"→excel, "digital"→git, "organisational"→go.
   Every job in the database had been scored that way.
2. **Resume tailoring was failing on almost every job.** Responses truncated at `max_tokens`,
   parsed as nothing, fell back to the deterministic document *after paying*. Worse, the
   fragment was cached. 0 of 4 real jobs used the model's document; now 4 of 4.
3. **Preferences were never stored**, so all the seniority/location/employment rules that
   existed and were tested never fired. Senior USA architect roles ranked at 79% for someone
   wanting a Bangalore internship.
4. **Role matching treated a neighbouring job family as a total mismatch**, multiplying a
   perfect-skills job down to a third of its value.
5. **The rescore report blamed the wrong cause twice** — told the user a Data Analyst role
   "no longer matches Python" when it had simply aged out.
6. **The event bus accumulated a `DatabaseSink` per app startup**, so every event was written
   N times after a reload.
7. **Aliases were counted as separate matched skills**, inflating scores for alias-rich Fact
   Banks.

---

## Top remaining priorities

Ranked by what unblocks the most value per unit of work.

1. **Get the three free API keys.** Not code. Everything downstream is operating on thin
   regional data.
2. **Discovery volume is the binding constraint — measured, not assumed.** Of 1,529 open
   jobs, exactly **2** survive scoring. The refusals are 1,012 too old, 325 seniority,
   177 weak score, 13 employment type. But relaxing the filters barely helps, and this
   was measured by rescoring the whole corpus against each altered preference:

   | change | jobs surviving |
   |---|---|
   | today (age 30, entry band) | 2 |
   | age limit 60 | 9 |
   | age limit 90 | 13 |
   | age limit off entirely | 13 |
   | experience band off entirely | 14 |
   | age 90 **and** band "mid" | 24 |

   Turning **every** filter off yields 24 jobs. The scorer is not too strict; there are
   not enough relevant entry-level roles in the corpus. A sweep of all 79 boards, with the
   schedule forced, returned only 14 new openings and 9 closures — the boards are being
   read properly and simply do not hold much more.

   A discovery campaign across Bangalore, Chennai, Hyderabad and Pune saw 305 postings and
   54 companies, of which 5 were applyable and 0 worth applying to. The keyless sources
   (TheMuse, Remotive, Arbeitnow) are remote- and US-centric; they return Lemon.io, A.Team
   and Coalition Technologies rather than Indian employers. Jooble now contributes since
   the location fix but resolves to country level.

   **The fix is two keys, not code.** Adzuna is the only source that covers Indian cities
   properly, and the RapidAPI key is not subscribed to JSearch (HTTP 404 on every call).
   Everything downstream is operating on a corpus that cannot support it.
3. **Recalibrate ranking thresholds, or research the shortlist (§23).** Nothing can currently
   reach "high priority". Either is a short job; the second is better.
4. **Contact discovery + Gmail outreach (§41, §43).** The whole Outreach column is at 25%
   because there is no contact data and no transport. This is the largest missing *feature*.
5. **Multi-user + auth (§1, §60).** The largest structural gap. Touches every table.
6. **Next.js frontend (§49, §63).** The current dashboard works; this is the specified one.
7. **Search provider layer (§12).** Unblocks real company discovery independent of job boards.
8. **Apify / Vibe / Clay (§9–11).** Paid, and only worth it after §12 exists.
9. **MCP (§36), LangSmith (§63), Redis (§61).** Infrastructure; none blocks a user outcome.
10. **Portal adapters (§17).** Pending your decision on the human-login approach.

---

## The honest summary

What exists is unusually careful about the thing that matters most — it will not lie about
you. The Fact Bank, the validators, the pre-submit contradiction check and the tool boundary
are real, tested, and have each caught a genuine fabrication in this session.

What exists is **not yet a product**: one user, SQLite, no auth, no queue, no observability,
and a dashboard that is not the specified stack.

And the biggest practical gap is not code at all — it is that the agent is
**intelligent but under-fed**. Three free API keys change more than the next three features.
