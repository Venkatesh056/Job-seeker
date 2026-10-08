# Build status — what exists, and what comes next

Written 2026-09-15. Measured against the running system, not against intent.

**1,300 tests passing · ruff clean · mypy clean across 137 source files · 27 tables**

Live data right now:

| | |
|---|---|
| Companies | 66 |
| ATS boards | 24 |
| Jobs | 1,748 (all open) |
| Jobs within 30 days | **735** |
| Scored | 1,748 — 6 review, 19 low, 1,723 reject |
| Candidate facts | 49 |
| **People** | **91** across 8 companies |
| — worth contacting (≥45) | **29** |
| — with a LinkedIn profile | 47 |
| — with a published email | 0 |

---

## Part 1 — What is built

### The spine, verified end to end

**Resume → Fact Bank.** PDF/DOCX/TXT parsing, section detection, every extraction category.
Each fact records the resume line it came from. Re-verified today out of necessity: the
resume was re-imported after the database incident and produced 49 facts with evidence
locators intact.

**Company → careers page → ATS → jobs.** 14 ATS adapters (Greenhouse, Lever, Workday,
Ashby, SmartRecruiters, Workable, Recruitee, BambooHR, Teamtailor, Personio, Rippling,
Keka, Breezy, Freshteam), slug guessing with a careers-page fallback. This is what rebuilt
1,748 jobs from 24 boards with no paid provider at all.

**Job-specific resume optimization.** Exact JD, versioned renders, each application linked
to the exact resume version used. Fixed earlier this project: it was tailoring twice and
paying twice.

**Company-level application.** Plan before act. Verified against Quantiphi: 41 openings, 6
newly discovered, 1 applyable at 0.75, 40 refused with individual reasons. The daily cap
binds *inside* the loop and is reported when it holds anything back. Nothing submits.

**Search provider layer.** Tavily + SerpAPI, cheapest-budget-first (1,000 credits vs 250
searches), failover, quota accounting. `harvest()` turns a search into an ATS board that is
then read free forever.

**LLM router.** Provider abstraction, task routing, fallback, token and cost tracking.

**Checkpointing.** Proven across two processes: one parked an approval and exited, a fresh
interpreter resumed it and submitted with confirmation text.

### Built in the last stretch

**Database safety** — `storage/safety.py`, `storage/backup.py`, `tests/conftest.py`.
Two conditions must both hold before anything destructive runs: `CAREERAGENT_ENV=test` and
a disposable URL. Development is deliberately excluded. The suite-wide conftest pins the
environment, defaults the URL to a temp file, strips every provider key, and **fails any
test that ends up pointed at a real database**. Snapshot/restore/prune/health on top.
`test_database_safety.py` reproduces the incident exactly and asserts the refusal.

**Unknown ≠ Zero** — `models/evidence.py`. Three states: `KNOWN`, `UNKNOWN`,
`NOT_APPLICABLE`. An unknown component surrenders its weight instead of contributing a
zero. Every score now carries `confidence`, `evidence_coverage` and `unknown_fields`, so a
0.9 built on one signal is distinguishable from a 0.9 built on six. This bug was found
three separate times before it was fixed at the root.

**Apollo as a company-facts source** — `sources/apollo.py`. Its free plan refuses people
search (403), so it is used for company enrichment and job postings instead. Found 211 real
jobs keyed on domain. Credit exhaustion is now recognised (`BILLING.LIMIT.CREDITS_EXHAUSTED`
arrives as HTTP 422, the same status as a malformed request) and the Keys page reports
`exhausted` rather than trusting `auth/health`, which returns healthy for a spent account.

**Description fetching** — `pipeline/describe.py`. Apollo postings arrive with no body
text, so they matched zero skills. Now fetches pages for postings that could plausibly
survive scoring; of 192 candidates only 27 were worth a request, and scores moved in both
directions.

**Gmail transport** — `outreach/gmail.py`. Scope is `gmail.send` **only** — it cannot read
the mailbox. Token refresh preserves the refresh token. `deliver()` applies every existing
guard then records Gmail's message id as evidence; a failed send leaves the record
*approved*, not sent.

**People intelligence** — `models/person.py`, `people/relevance.py`, `people/discover.py`.
Detailed below.

---

## Part 2 — Company People & Outreach Intelligence

### What is built

**The no-fabrication rule is structural, not advisory.** `ContactDetail` has no constructor
that omits provenance — `ContactDetail(value="guess@acme.com", kind=TEAM_PAGE,
source_url="")` **raises**. There is nowhere in the codebase to put an email nobody
published. Given "Rahul Kumar" at `acme.com`, `rahul.kumar@acme.com` is a plausible guess,
and a plausible guess beside real data is indistinguishable from it.

**Career-aware relevance.** Seven components — function, authority, proximity, seniority,
technical, reachability, history. Measured output for an entry-level ML opening:

| Person | 3,000-person company | 12-person startup |
|---|---|---|
| AI Engineering Manager | **96/100** | 96/100 |
| Head of Data | 84/100 | 84/100 |
| VP Engineering | 69/100 | 69/100 |
| Technical Recruiter | 67/100 | 67/100 |
| Chief Executive Officer | **12/100** | **48/100** |
| Finance Manager | 8/100 | 8/100 |

The AI Engineering Manager at 96 and the Finance Manager at ~10 match the specification's
own worked example. The CEO moving 12 → 48 with company size is the calibration that stops
this being a seniority ladder.

**Entity resolution.** LinkedIn URL or work email is identity. A matching name alone is
never enough — two people called Rahul Kumar can work at the same company, and a wrong
merge means a message addressed to one describing the other's job. Merge keeps the
better-sourced detail: a company's own page beats an enrichment provider on the same fact.

**Email classification.** `careers@`, `hr@`, `careers-india@`, `hr.team@` are departments;
`rahul.kumar@` is a person. The distinction changes how a message should be written.

**Discovery from company pages.** 13 candidate paths, robots.txt honoured, conservative
extraction requiring a name adjacent to a recognisable job title.

### What discovery actually yields — and this matters

*(Superseded by Phase 2 — kept because the reasoning still holds and the rules below are
still the ones doing the filtering.)*

Measured against eight real company sites: **2 people found, both real, zero fabricated.**
After Phase 2 this became **91 people across 8 companies, 29 of them worth contacting.**

An earlier, looser version returned "Zoho Expense", "Agent Mode", "Partner Awards" and
"Postman MCP Server" as people. Four rules closed that:

* a product word in the name disqualifies it
* a title must contain a **role** word, not merely mention the field — "AI Agents
  Engineered" and "Head of AI" both contain "ai"
* a title belongs to exactly one person, so a card cannot steal its neighbour's
* a contact detail window starts at the person's own block — reaching back one attached
  Arjun Mehta's LinkedIn profile to Ravi Nair

**What looked like a limitation was a bug.** The diagnosis at the time — "these pages are
JavaScript-rendered" — was half right and led to the wrong conclusion. See Phase 2: the
pages existed, at URLs the crawler was never asking for.

---

## Part 3 — What remains, in phases

Ordered by value per unit of work, not by section number.

### Phase 1 — Make the people feature usable ✅ **done**

* `PersonRecord` and `PersonSource` tables, migration `fb250cab12e4` (27 tables). Contact
  details store their source, verification state and confidence as columns rather than as
  bare strings — there is nowhere in the schema to put an address with no provenance.
* `people/store.py` — save, update, filter, and record relevance *with the job it was
  computed for*, because relevance is not a property of a person.
* `POST /companies/{id}/people` — discover, rank, persist. `GET /companies/{id}/people`.
* `GET /people` with every filter from the spec, plus `/people/functions` so a filter
  cannot mistype a value.
* A **People** dashboard page. Relevance leads; every address shows its verification and a
  link to the page it came from; anything unverified says "not sent automatically".

**Three bugs found and fixed while building it.**

*The target role was always "Engineer".* The fallback read a `job_title` fact the resume
parser never produces. It now asks the career detector, which already knew: **"Machine
Learning Engineer"**.

*A weaker later sighting could downgrade a verified address.* A thinner crawl would have
silently destroyed a good record. Verification now wins over recency.

*`row.sources` on a freshly-flushed row raised `MissingGreenlet`* — a lazy load inside an
async session. This would have hit production, because the route calls `save` the same way.
Source URLs are now fetched with an explicit query.

**UI verified** with a rendered payload: 96/100 AI Engineering Manager with a verified
address and a source link; 67/100 Technical Recruiter whose `careers@` address is labelled
"unverified · a department, not a person" and marked *not sent automatically*; 8/100
Finance Manager with "none published".

### Phase 2 — Fix the discovery yield ✅ **done, but not the way it was planned**

The plan was Apify. I measured Apify first, and it turned out not to be the answer —
though what I learned from it was.

**What Apify told me.** A rendered crawl of `quantiphi.com/leadership` cost **$0.0042** and
took 33 seconds, so the budget was never the problem — $5 buys ~1,180 pages. But the result
came back with `loadedUrl: "https://quantiphi.com/"`. **The page does not exist.** It 302s
to the homepage, which answers 200 with `text/html`, so my crawler had been parsing the
*homepage* and reporting its marketing copy as staff. Every "person" it found there was a
section heading or a customer testimonial.

**Three fixes, in the order they were found.**

1. **Reject a redirected page.** A guessed path that lands somewhere else is not the page
   we asked for. This alone took the yield to zero — honestly zero, where before it had
   been two people and several fictions.

2. **Read the site's own links** instead of guessing paths. Measured: sigmoid.com has 196
   anchors and its only "About" link is `href="#"`, because modern navigation is a
   JavaScript dropdown. This found nothing either — but it is correct, and it works on
   sites that do publish their navigation.

3. **Ask a search provider where the page actually is.** This is the one that works.
   Search engines have already rendered these sites, so they know the real URL:
   `quantiphi.com/company/executive-team` and `razorpay.com/newsroom/leadership-team` —
   neither of which any path list would produce. One Tavily credit per company, against a
   1,000-credit monthly budget.

**Result: 15 real people across 6 companies**, up from 2 across 8, with no fabrications.
Harshil Mathur (Razorpay CEO & Co-founder), Mayur Rustagi (Sigmoid CTO), Jim Reesing
(Quantiphi CEO), and twelve more — every one a genuine executive.

Three further defects closed on the way: "View More" and "Top Trending" were being read as
names; a press release announcing one appointment was being parsed as a page of people; and
Mayur Rustagi appeared twice because "CTO and Co-Founder" and "Chief Technology Officer and
Co-Founder" do not contain one another. Titles now compare by function and level, so one
man is one record.

**And one bug worth naming:** the anchor regex contained `` written into a non-raw
string, which made it a literal backspace character — `<a` can never match. Link
discovery was silently dead, and the 15 people were found by search alone. Fixed, and the
pattern is now asserted in a test.

**Two more bugs, found only by running it across the real corpus.**

*`"ai"` matched the start of `"Aiven"`.* So `function_of("Chief Financial Officer, Aiven")`
returned `ai_ml`, and Aiven's CFO scored **85/100** for a machine-learning candidate — as
did its VP of People and its Chief Revenue Officer. Exactly the same class of bug as `cto`
matching inside `dire(cto)r`: a leading word boundary had been added then, and this is the
trailing half of the same fix. Short markers now need a boundary at both ends; longer ones
stay open because several are deliberately prefixes (`recruit` must reach "recruiter",
`financ` must reach "financial").

*`"Chief Strategy Officer"` was stored as a person.* Its subject matter is in no function
vocabulary, so the existing "a title is not a name" test did not catch it. A phrase
containing a role word is now never a name.

**Apify is not wired in, deliberately.** It works and it is cheap, but search solved the
problem for a tenth of the effort and none of the credit. The $5 stays unspent until
something genuinely needs rendering — a site search cannot reach, most likely.

### Phase 3 — Outreach that closes the loop *(next)*

* Personalised **email** agent: reads candidate, job, company, person's role, prior
  contact; picks the resume; adapts to recruiter vs engineering manager vs CTO
* Separate **LinkedIn** message agent — shorter, no attachment, never a copy of the email
* Subject-line generation with several candidates and a choice
* Cross-channel timeline and duplicate protection
* Opt-out, unsubscribe, bounce, suppression list
* Campaign object with per-campaign limits and approval mode
* Outreach status machine: `Not Contacted → Draft → Awaiting Approval → Approved → Sent →
  Replied → Follow-up Due → Interested / Not Interested / Opted Out / Bounced`

### Phase 4 — Knowledge graph completion

`Person`, `Lead`, `Outreach`, `Interview`, `Offer` nodes with `HAS_PERSON`, `HAS_ROLE`,
`WORKS_IN`, `RELEVANT_TO`, `CONTACTED`, `TARGETS` edges. Six named agent queries, including
"hiring managers at matched companies I have not contacted" and "which resume versions
produce the best outcomes".

### Phase 5 — Multi-user and authentication

The largest structural gap. `user_id` on every entity, tenant isolation, Supabase auth,
explicit cross-user isolation tests. Touches every table.

### Phase 6 — Infrastructure

Redis queues and rate limits, LangSmith tracing, Supabase storage for resumes and
screenshots, systematic failure-injection suite.

### Phase 7 — Next.js frontend

The specified stack. The current plain-HTML dashboard works; this is a migration, not a
rescue, and belongs last for exactly that reason.

---

## Blocked, and by whom

| Item | Nature | Who |
|---|---|---|
| Adzuna keys | User action — free, 5 min | You |
| JSearch 404 | User action — subscribe existing key, 1 min | You |
| Apollo credits | Provider limit — exhausted this cycle | Resets |
| Apollo people search | Provider limit — not in free plan | Paid tier, or Hunter.io |
| Gmail sending | User action — one-time consent | You |
| Vibe Prospecting | Provider limit — host does not resolve | Send the docs URL |
| LinkedIn automation | **Platform limitation** — terms prohibit it | Nobody; assisted only |

See `required_api_keys.md` for every key, its free tier, and what it buys.
