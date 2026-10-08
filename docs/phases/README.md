# Phases — the master index

Every phase, what it covers, and where it stands. Each completed phase has its own document
with the measurements and the bugs found; this page is the map.

**Backend: 1,480 tests passing · ruff clean · mypy clean · 155 source files · 31 tables**
**Frontend: Next.js 16 · 10 routes · 15 tests · build, eslint and tsc clean**

| # | Phase | Status | Document |
|---|-------|--------|----------|
| 0 | Foundations — resume, ATS, tailoring, company apply, safety | ✅ Complete | [04-master-audit.md](../04-master-audit.md) |
| 1 | People foundation — model, relevance, storage, API, screen | ✅ Complete | [phase-1-people-foundation.md](phase-1-people-foundation.md) |
| 2 | Discovery yield — finding the page rather than guessing | ✅ Complete | [phase-2-discovery-yield.md](phase-2-discovery-yield.md) |
| 3 | Outreach that closes the loop | ✅ Core complete | [phase-3-outreach.md](phase-3-outreach.md) |
| 4 | Knowledge graph completion | ✅ Complete | [phase-4-knowledge-graph.md](phase-4-knowledge-graph.md) |
| 5 | Multi-user and authentication | ✅ Complete | [phase-5-multi-user.md](phase-5-multi-user.md) |
| 6 | Infrastructure — rate limits, storage, tracing, failure injection | ✅ Complete | [phase-6-infrastructure.md](phase-6-infrastructure.md) |
| 7 | Next.js frontend | ✅ Complete (read-heavy half) | [phase-7-frontend.md](phase-7-frontend.md) |
| 8 | Closing the gaps — fact review, concurrency, approvals | ✅ Complete | [phase-8-closing-gaps.md](phase-8-closing-gaps.md) |

---

## Live data right now

| | |
|---|---|
| Companies | 66 · **53 researched** |
| ATS boards | 24 |
| Jobs | 1,748 (all open) |
| — joined to a company row | 876 |
| Jobs within 30 days | 735 |
| People | 91 across 8 companies |
| — worth contacting (≥45) | 29 |
| — with a LinkedIn profile | 47 |
| — with a published email | 0 |
| Suppressed contacts | 0 |
| Candidate facts | 49 · **0 confirmed — [/facts](../../frontend/src/app/facts/page.tsx) now makes this possible** |
| Stored objects | 20 (787 KB, local) |
| Frontend routes | 10 |
| Per-candidate relevance rows | 50 |
| Accounts | 1 (owns the profile; no password set yet) |

---

## ✅ Phase 0 — Foundations

Resume → Fact Bank with evidence locators. 14 ATS adapters. Job-specific resume
optimisation with versioned renders. Company-level application that plans before it acts.
Search provider layer with budget-first failover. LLM router. Cross-process checkpointing.

Also, after a test destroyed the production database: environment separation, a destructive
-operation guard that needs `CAREERAGENT_ENV=test` **and** a disposable URL, a suite-wide
conftest that fails any test pointed at a real database, and snapshot/restore/health.

And **Unknown ≠ Zero** — three states (`KNOWN`, `UNKNOWN`, `NOT_APPLICABLE`) after the same
bug was found three separate times. Every score now carries `confidence`,
`evidence_coverage` and `unknown_fields`.

## ✅ Phase 1 — People foundation

A contact detail that cannot exist without its source. Career-aware relevance where an
engineering manager beats a CEO 96 to 12 for an entry-level ML role, and the CEO rises to 48
at a twelve-person startup. Entity resolution that never merges on name alone. Storage, API,
and a People screen.

## ✅ Phase 2 — Discovery yield

Planned as Apify; solved by search. Found that guessed URLs were silently redirecting to
homepages, so the crawler had been reporting marketing copy as staff. **2 people → 91.**

## ✅ Phase 3 — Outreach that closes the loop *(core complete)*

**Done.** Suppression with four distinct reasons, checked at three boundaries — before
sending, before marking sent by hand, and before a draft is even written. Cross-channel
duplicate protection, because the recipient experiences two messages as two however the
sender files them. Personalised email and LinkedIn agents that write genuinely different
messages for four audiences, with template openings and fabricated rapport both refused.
Verified live: an 84-word email and a 57-word LinkedIn note to the same person, each citing
real Fact Bank ids, zero warnings.

**Remaining in this phase.** Campaign objects with per-campaign limits. Reply and bounce
detection, which need the `gmail.readonly` scope — a much broader ask than `gmail.send`,
deliberately left as a separate opt-in. The fuller status machine (`Delivered`,
`Follow-up Due`, `Interested`, `Not Interested`). An approval screen: the data is all there,
the screen is not.

## ✅ Phase 4 — Knowledge graph completion

Eight new node kinds and ten new edge kinds, so the graph can finally draw the people the
last two phases collected: `company → person → role → department`, and every sent message
pointing at the human who received it. Seven named questions, four of them new, including
"who at a company I match has not heard from me" and "which resume version actually drew a
reply".

Found the phase's real bug by asking one of them and getting an empty answer:
**`job.company_id` was NULL on all 1,748 rows** — the people half and the jobs half of the
database had never been joined, and an empty result from a broken join is indistinguishable
from an empty result from an empty database. Fixed forward in `upsert_jobs` and backward
with `careeragent relink`; **876 jobs now joined**, and the question answers.

Rates below five submissions are withheld rather than rounded: one response from one
application is not a 100% response rate.

## ✅ Phase 5 — Multi-user and authentication

Billed as the largest structural gap. It was not, and the reason is the finding: **the schema
was never single-user** — `candidate_id` has been on every per-person table since the first
migration. What made it single-user was one line, `select(Candidate).limit(1)`, with 111 call
sites downstream. The lookup changed rather than its callers.

Accounts, scrypt passwords and HS256 sessions, all on the standard library — no dependency
was added. The tenancy fallback is now **a refusal**: with identity enforced and nobody
bound, database work raises rather than quietly reading the first row, because the quiet read
is a cross-tenant disclosure and the exception is a bug report.

Two bugs, both caught by the tests rather than by reading. The auth dependency was attached
to the routers, so `/api/v1/candidate` — the parsed resume — served anonymous callers,
because `api.py` declares its own endpoints. And revocation compared whole seconds, so
logging out in the same second as logging in did nothing; tokens carry a serial now, and
there is no clock left in it to lose a race with.

Verified in a browser on a two-account deployment: Alice sees haskell, Bob sees fortran,
neither sees the other, and no token is 401.

## ✅ Phase 6 — Infrastructure

Outbound rate limiting (token bucket per provider, Redis-backed when configured), an object
store, LangSmith tracing that admits when it is off, and a fault-injection harness that runs
the **real** client code against a hostile `httpx` transport.

The fault suite was expected to be the boring item. It found two real bugs in an hour.

A board claiming `totalFound: 40` that kept returning the same three rows produced
**42 jobs from 3 openings, `ok=True`, 14 requests** — nothing raised, because nothing was
broken from the loop's point of view. Now 3 rows, flagged `partial` with the shortfall, in 2
requests. Verified against the live APIs that genuine pagination still works.

And the resume upload used `file.filename` as a path: `../../../pwned.pdf` ends in `.pdf`,
passed the allowlist, and **wrote outside the storage directory**. The fix is a generated
key, not a sanitiser — a sanitiser is something you have to keep getting right.

**Redis queues were deliberately not built.** Redis backs the rate limiter, which is a real
cross-process need. Queues would be speculative: one asyncio process, durable checkpoints
already surviving restarts, nothing distributed. The doc says what would change that.

## ✅ Phase 7 — Next.js frontend

Next.js 16, React 19, Tailwind 4, eight routes, organised around one idea: **nothing on
screen may look more certain than the data behind it.** The backend reports every score
with how much of it rests on evidence, so the measurement component draws three regions —
measured, unmeasured, headroom — and the hatched band is the part nobody could measure. An
empty track reads as zero; a shorter track reads as certainty; neither is true.

The same rule everywhere: an em dash instead of `0%`, three distinct states for loading /
failed / genuinely empty, no rate quoted below five submissions, and **contactable ≠
reachable ≠ no channel** on the People screen — because zero of the 91 people have a
published email and one green tick would hide that.

The graph is a hand-rolled canvas where hover dims everything that is not a neighbour; at
118 nodes that is the difference between a picture and a hairball. ⌘K searches across
openings and people at once.

Four bugs found while building, including a `w-full` on the score bar that won the cascade
against its caller and squeezed company names to zero characters — the names were in the
DOM the whole time.

**Sending, approving, submitting and uploading were deliberately left on the old
dashboard.** A second send button built during a visual migration is a second way for a
message to go out by accident.

## ✅ Phase 8 — Closing the gaps

Not a planned phase. It came from asking *"what's remaining?"* and getting an answer whose
headline was not a code gap: **the system had never been run to an outcome.** Running it
found four real bugs, three of which only appear when somebody actually uses it.

**The Fact Bank had no way to confirm a fact.** `verified` has been documented as
human-review-only since the first migration, and nothing anywhere could set it — the facts
endpoint did not even return an id. 0 of 49 was unreachable, not neglectful.

**The database locked itself out.** The research pass wrapped sixty-six website reads in one
transaction — a write lock held for minutes across HTTP calls — and SQLite's defaults (no
WAL, zero busy timeout) turned that into an error for every other process. Demonstrated
rather than theorised: interrupting the run rolled back every profile it had gathered.

**Foreign keys were never enforced.** Turning them on failed 7 tests inserting applications
against jobs that did not exist. Every `ON DELETE CASCADE` had been decoration.

**Relevance was somebody else's** — the Phase 5 leak, now keyed on (person, candidate).

Also: the approval screen Phase 3 named, renders addressable by key rather than by an
absolute Windows path, and the frontend tests Phase 7 named. **53 of 66 companies
researched**, up from zero.

---

## Blocked, and by whom

| Item | Nature | Who unblocks |
|---|---|---|
| Adzuna keys | User action — free, 5 minutes | You |
| JSearch 404 | User action — subscribe the existing key | You |
| Apollo credits | Provider limit — exhausted this cycle | Resets |
| Apollo people search | Provider limit — not in the free plan | Paid tier, or Hunter.io |
| Gmail sending | User action — one-time consent | You |
| Supabase auth | User action — free project, 5 minutes | You |
| Vibe Prospecting | Provider limit — host does not resolve | Send the docs URL |
| LinkedIn automation | **Platform limitation** — terms prohibit it | Nobody; assisted only |

See [required_api_keys.md](../../required_api_keys.md) for every key and its free tier.
