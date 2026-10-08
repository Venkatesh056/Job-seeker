# Repository Research — AI Career Agent

**Method.** All 31 repositories in the brief were probed against the GitHub API (metadata,
stars, language, last push, license), all 31 READMEs were downloaded and keyword-indexed, and
16 were shallow-cloned and read at source level. ATS endpoints referenced by the corpus were
called live to separate working infrastructure from documentation. Findings below are marked
**[code]** where verified in source and **[readme]** where only claimed.

Research date: 2026-09-12.

---

## 1. Headline findings

1. **All 31 repositories exist and are real.** None were fabricated; none are archived.
2. **16 of 31 carry no license at all** — that is *all rights reserved*, not public domain.
   Their code cannot be copied, including several the brief ranks Tier S. See §5.
3. **README claims substantially exceed implementation in most of the corpus.** Several
   projects advertising "6 AI agents" and "6 ATS boards" resolve to a 2–3 node graph and a
   keyword scorer. The median project is ~5K LOC written by one person over a few weeks.
4. **The single most valuable reusable asset in the corpus is the smallest repository**
   (`shunsukefuruyama/ats-jobs`, 1,754 LOC, MIT): verified, unauthenticated ATS job-board API
   readers for 12 platforms plus robots.txt-respecting slug discovery.
5. **The hard problem in job discovery is not the endpoint — it is resolving a company to its
   board slug.** Confirmed empirically in §3.
6. **The two best safety ideas in the corpus are both about *not* submitting**: a pure
   pre-submit verdict function over gathered DOM state (Malik1942) and a replayable form
   fixture with a tripwire oracle asserting the submit button was never activated (neonwatty).
7. **No repository in the corpus has a credible answer to portal Terms of Service.** Every
   project that automates LinkedIn, Indeed, Naukri or Glassdoor does so in violation of those
   platforms' user agreements. This is the dominant risk in the brief and is addressed in
   `02-roadmap.md`.

---

## 2. Repository inventory (verified metadata)

Sorted by verified source LOC. `LOC` counts `.py/.ts/.tsx/.js/.jsx` excluding `node_modules`,
`dist`, `.next`, `venv`. Blank = not cloned (README-only assessment).

| # | Repository | Stars | Lang | Last push | License | LOC | Verified depth |
|---|---|---|---|---|---|---|---|
| 22 | nishilbhave/ats-resume-tailor | 2 | Shell | 2026-03-07 | MIT | 0 | code (prompt text only; no program) |
| 15 | shunsukefuruyama/ats-jobs | 0 | JS | 2026-08-26 | MIT | 1,754 | code (full read) |
| 8 | ajaykallepalli/resume-agent | 0 | Python | 2026-03-30 | MIT | 2,211 | code |
| 14 | erichulam/job-application-agent | 0 | Python | 2026-04-02 | **none** | 2,352 | code |
| 23 | JaimeYeung/Resume-Tailor-AI | 37 | TS | 2026-04-20 | **none** | 3,364 | code |
| 7 | BhairavJShah/JobPilot-AI | 2 | Python | 2026-08-19 | MIT | 4,918 | code |
| 21 | Nils1511/AI-Job-Application-Agent | 0 | Python | 2026-04-05 | **none** | 5,679 | code |
| 18 | 17nbist/jobApplier | 11 | JS | 2026-07-19 | **none** | 7,356 | code |
| 6 | AyushSinghRana15/NexApply | 1 | Python | 2026-07-19 | **none** | 9,241 | code |
| 16 | Malik1942/job-agent | 3 | Python | 2026-07-31 | **none** | 12,696 | code (deep read) |
| 1 | kvcops/JobHunterX | 5 | Python | 2026-08-08 | **none** | 17,202 | code (deep read) |
| 20 | neonwatty/job-apply-plugin | 107 | Python | 2026-09-11 | MIT | 19,283 | code (deep read) |
| 30 | TheCromazone/Personal-JobPilot | — | — | — | MIT | 26,763 | code (structure) |
| 3 | jaimaann/LangHire | 101 | Python | 2026-08-09 | MIT | 27,039 | code (structure) |
| 17 | privacydied/job-application-skill | 1 | Python | 2026-09-01 | MIT | 44,213 | code (structure) |
| 4 | suxrobgm/jobpilot | 67 | TS | 2026-09-10 | MIT | 65,516 | code (structure) |
| 2 | slowloris-98/HireShire | — | — | — | **none** | — | readme |
| 5 | vivekshekharrai-del/AIApplyJobs | 0 | Python | 2026-03-08 | **none** | — | readme (31 KB repo) |
| 9 | Praneeth0526/Job_Agent | 1 | Python | 2025-11-07 | Apache-2.0 | — | readme (stale ~10 months) |
| 10 | Nawaz-B-04/job-automation | 3 | Python | 2026-03-22 | **none** | — | readme |
| 11 | Keerthana-tech-26/job-application-agent | 0 | Python | 2026-09-01 | **none** | — | readme |
| 12 | sudheerbez/job-apply-agent | 1 | Python | 2026-04-02 | **none** | — | readme (29 KB repo) |
| 13 | SrMarinho/jobpilot | 1 | Python | 2026-08-29 | **none** | — | readme |
| 19 | Thesirloc/job-autofill | 0 | JS | 2026-05-17 | **none** | — | readme |
| 24 | workopia/ai-resume-tailor | 2 | TS | 2026-04-15 | MIT | — | readme (13 KB repo) |
| 25 | rotsl/resume-tailor | 4 | Python | 2026-07-12 | MIT | — | readme |
| 26 | simaqian/resume-tailor | 0 | TS | 2026-06-15 | MIT | — | readme |
| 27 | nuin/resume-tailor | — | — | — | MIT | — | readme |
| 28 | ShipItAndPray/job-apply-ai | — | — | — | **none** | — | readme |
| 29 | praneethravuri/job-assistant | — | — | — | **none** | — | readme |
| 31 | MisterStranger03/JobPilot | — | — | — | **none** | — | readme |

Six rows show `—` for stars/push because GitHub rate-limited the unauthenticated API mid-sweep;
their licenses were recovered from raw file fetches. Repo size from the API is noted where it
materially qualifies a claim (e.g. #5 advertises five portals in a 31 KB repo).

---

## 3. Live verification of ATS job feeds

The brief's §5 asks to prefer official/public feeds over scraping. That premise checks out —
but only partly, and the failure mode matters. Each endpoint below was called live,
unauthenticated, on 2026-09-12.

| Platform | Endpoint shape | Live result |
|---|---|---|
| Greenhouse | `GET boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` | **200, 635 jobs** (stripe) |
| Ashby | `GET api.ashbyhq.com/posting-api/job-board/{slug}` | **200, 145 jobs** (ramp) |
| Lever | `GET api.lever.co/v0/postings/{slug}?mode=json` | **200, 311 jobs** (palantir) |
| Rippling | `GET api.rippling.com/platform/api/ats/v1/board/{slug}/jobs` | **200, 656 jobs** |
| Workday | `POST {host}/wday/cxs/{tenant}/{site}/jobs` body `{limit:20,offset}` | **200, paginated** |
| Personio | `GET {slug}.jobs.personio.de/xml` | **200, XML** |
| SmartRecruiters | `GET api.smartrecruiters.com/v1/companies/{slug}/postings` | 200 but **0 results on 5/5 guessed slugs** |
| Workable | `GET apply.workable.com/api/v1/widget/accounts/{slug}?details=true` | 200-empty or 404 on 4/4 guesses |
| Recruitee | `GET {slug}.recruitee.com/api/offers/` | 404 on 4/4 guesses |
| Teamtailor | `GET {slug}.teamtailor.com/jobs.json` | 404 on guess |

**The finding that matters:** where a guessed slug was right, the feed returned hundreds of
fully-structured jobs with descriptions — no scraping, no auth, no bot defences. Where the slug
was wrong, several platforms returned `200 OK` with an empty list rather than a 404 —
indistinguishable from "this company has no openings" unless you know better. SmartRecruiters,
Workable, Lever, Teamtailor and BambooHR all behave this way.

`ats-jobs` handles this explicitly with an `emptyMeansExists` flag per provider plus a
careers-page discovery fallback that regex-matches embedded board URLs and JS widget attributes
(`data-board-token`, `data-ashby-job-board`, `data-job-board-id`) — reading `robots.txt` first
and skipping disallowed paths. Two further traps it documents, which we inherit: Lever, Rippling
and Breezy all return bare JSON arrays, so response *shape* cannot identify the platform (it
checks a platform-specific key instead — mistaking one for another yields a pile of jobs with
empty titles, failing silently and plausibly); and Recruitee 404s without its trailing slash.

**Conclusion:** ATS feeds are a genuine, robust, ToS-clean discovery channel — and company→slug
resolution, not HTTP, is the engineering problem to solve. This inverts the brief's implicit
priority and is reflected in the MVP scope.

---

## 4. Feature matrix

Derived from mechanical keyword indexing of all 31 READMEs, then corrected against source for
the 16 cloned repos. **●** = verified in code. **○** = claimed in README, unverified.
**·** = absent.

| Feature | JobHunterX | LangHire | NexApply | JobPilot-AI | Malik1942 | neonwatty | ats-jobs | privacydied | Cromazone | suxrobgm |
|---|---|---|---|---|---|---|---|---|---|---|
| LinkedIn | ● | ● | · | ○ | ● | ● | · | ○ | ○ | ○ |
| Indeed | ● | ● | ○ | ○ | ● | · | · | ○ | ○ | ○ |
| Naukri | ● | ● | ○ | ○ | · | · | · | · | · | · |
| Internshala | · | · | ○ | · | · | · | · | · | · | · |
| Glassdoor | ○ | ○ | · | · | · | · | · | · | · | · |
| Greenhouse | ● | ● | · | · | ● | ● | ● | ● | ○ | · |
| Lever | ● | ● | · | · | ● | ● | ● | ● | ○ | · |
| Workday | · | ● | · | · | ● | ● | ● | ● | ○ | · |
| Ashby | ● | · | · | · | ● | ● | ● | ● | ○ | · |
| Other ATS feeds | 3 | 4 | · | · | 3 | 1 | **8** | 4 | 2 | · |
| Resume tailoring | ● | ● | ● | ○ | ● | · | · | ● | ○ | ○ |
| Fact bank / no-fabrication | ● | · | · | · | · | · | · | ● | · | · |
| JD → structured extraction | ● | ● | ● | ○ | ● | · | · | ● | ○ | · |
| Deterministic scoring | ● | ○ | ● | ○ | ● | · | · | · | ○ | · |
| Semantic / embedding match | ● | · | · | · | · | · | · | · | · | · |
| Company research | · | · | · | · | · | · | · | · | ○ | · |
| LLM provider routing | ● | ● | ○ | ○ | ○ | · | · | · | ○ | · |
| Browser agent | ● | ● | ● | ○ | ● | ● | · | ● | ○ | ○ |
| Pre-submit verification | · | · | · | · | **●** | **●** | · | ○ | · | · |
| Human approval | ● | · | ● | ○ | ● | ○ | · | ● | ○ | ○ |
| Live browser view | ○ | · | · | · | · | · | · | ○ | · | · |
| Record/replay form tests | · | · | · | · | · | **●** | · | · | ○ | · |
| Duplicate detection | ● | · | ● | · | ● | ● | · | · | ○ | · |
| Application tracking | ● | ○ | ● | ○ | ● | ● | · | ● | ○ | ○ |
| Recruiter outreach | · | · | · | ○ | · | · | · | · | ○ | · |
| Analytics | · | ○ | ○ | ○ | ○ | ○ | · | · | ○ | ○ |
| Learning from outcomes | · | · | · | · | · | · | · | · | · | · |
| Answer memory | ● | ○ | ● | ○ | ● | ● | · | ● | · | · |
| Tests | ○ | · | · | · | ● | ● | ● | ● | ● | ● |

**Nothing in the corpus implements a learning/feedback loop over application outcomes.**
Several READMEs gesture at it; no repository closes it. Likewise, company intelligence as a
separate scored dimension is absent everywhere. Those two are genuinely greenfield.

---

## 5. Licensing — the constraint that shapes reuse

| License | Count | Repos |
|---|---|---|
| MIT | 13 | LangHire, suxrobgm/jobpilot, JobPilot-AI, resume-agent, ats-jobs, job-application-skill, job-apply-plugin, ats-resume-tailor, ai-resume-tailor, rotsl, simaqian, nuin, Personal-JobPilot |
| Apache-2.0 | 1 | Praneeth0526/Job_Agent |
| **None** | 17 | JobHunterX, HireShire, AIApplyJobs, NexApply, job-automation, Keerthana, job-apply-agent, SrMarinho, erichulam, **Malik1942**, jobApplier, job-autofill, Nils1511, **Resume-Tailor-AI**, ShipItAndPray, job-assistant, MisterStranger03 |

A repository with no LICENSE file is under exclusive copyright. Public visibility on GitHub
grants the right to view and fork *within GitHub*, not to copy code into a separate product.

This directly affects four projects the brief leans on most: **JobHunterX** (closest overall
architectural match), **Malik1942/job-agent** (best browser-safety code), **Resume-Tailor-AI**
(the Fact Bank concept the brief wants to adopt), and **17nbist/jobApplier** (50+ ATS field
configs). All four are **read-for-ideas only** — their *approaches* are not copyrightable, but
their code is. Where this document recommends one of their patterns, it means reimplement from
the described behaviour, not port the file.

---

## 6. Component classification

### REUSE — take the code (license permits)

| Component | Source | License | Note |
|---|---|---|---|
| 12 ATS feed readers (endpoints, parsers, `emptyMeansExists`, slug variants) | ats-jobs `src/providers.js` | MIT | Port to Python; keep the MIT notice. Verified live. |
| Careers-page ATS discovery (regex signatures, widget attrs, robots.txt gate) | ats-jobs `src/discover.js` | MIT | The piece that makes "just give it a domain" actually work. |
| Parallel provider probing with per-provider retry/backoff | ats-jobs `src/fetcher.js` | MIT | 404 never retried; 429/5xx backed off. |
| Canonical form-field catalog (`contact.first_name`, `authorization.sponsorship`, …) | neonwatty `qa/contracts.py` | MIT | The decoupling layer between ATS DOM and candidate data. |
| Fixture schema + tripwire oracle (`finalActionActivations: 0`) | neonwatty `qa/` | MIT | Record a real form once, replay in CI forever, assert submit never fired. |
| PII scrubbing for recorded fixtures | neonwatty `qa/privacy.py` | MIT | Required before any fixture is committed. |

### INSPIRE — reimplement the pattern, do not copy

| Pattern | Observed in | Why it matters |
|---|---|---|
| Pure `presubmit_verdict(records) -> failures` over DOM state gathered by JS | Malik1942 `browser.py` | Unlicensed. Best idea in the corpus: JS *gathers*, Python *decides*, so the decision is unit-testable with no browser. Handles `<option disabled selected>` placeholders, sentinel values, combobox readback equal to its own placeholder, `aria-invalid`. |
| Never-fill lists: demographics/EEO, veteran, disability, sponsorship, access codes | Malik1942 `browser.py` | Unlicensed. A guessed answer here is both wrong and offensive. Always human. |
| Combobox commit verification ("wanted X, widget shows Y" → raise) | Malik1942 `browser.py` | Unlicensed. Typing into a combobox without checking what committed is the top silent-wrong-data bug. |
| Keyword pre-screen gate before spending LLM tokens (`>= 0.35`), batch ~10 jobs/call | JobHunterX `job_scorer.py` | Unlicensed. Turns 5,000 jobs into ~80 LLM-scored jobs — exactly the brief's funnel. |
| Provider fallback chains + disk cache + per-provider concurrency semaphore | JobHunterX `config/llm_router.py` | Unlicensed. The shape of the brief's §31, done right. |
| Fact Bank / evidence-indexed tailoring | Resume-Tailor-AI | Unlicensed. Concept only; the brief already specifies it well. |
| Declarative per-application spec JSON + accumulated field-knowledge notes | privacydied `apply_specs/`, `references/` | MIT, but written around one author's own job hunt — the idea transfers, the data does not. |

### REWRITE — build from scratch

Orchestration graph, candidate knowledge base and Fact Bank, hybrid scoring engine, company
intelligence, final opportunity ranking, application decision engine, outreach, analytics,
learning loop, dashboard, auth, persistence. Nothing in the corpus is close enough to adopt,
and these are where the product's value actually sits.

### DO NOT USE

| Repo | Reason |
|---|---|
| Praneeth0526/Job_Agent | Last push 2025-11-07 (~10 months stale); Selenium-based; 40 MB repo dominated by committed binaries. |
| vivekshekharrai-del/AIApplyJobs | 31 KB repo advertising five portals, five LLM providers and a full pipeline. Claims not backed by code volume. |
| sudheerbez/job-apply-agent | 29 KB repo advertising six ATS platforms plus LinkedIn/Indeed and a tracker. Same problem. |
| nishilbhave/ats-resume-tailor | Verified: contains no program (0 LOC). Prompt text and shell glue only. |
| Any LinkedIn / Indeed / Naukri / Glassdoor automation code, from any repo | Automating these platforms breaches their user agreements regardless of code quality. |
| All 17 unlicensed repos, *as code* | All rights reserved. |

---

## 7. What the corpus teaches that the brief does not already say

1. **Silent-wrong beats loud-broken as a failure mode, and is far more common.** Three separate
   projects independently converged on verify-after-action: read back what the combobox
   committed, re-scan required fields before submit, assert the submit button was never
   activated in tests. The brief's §36 ("never silently claim submitted") is right, and the
   corpus shows it must be enforced mechanically, not by convention.
2. **A canonical field vocabulary is the correct ATS abstraction** — not per-ATS fill scripts.
   `contact.first_name` maps once to the candidate KB and once per ATS to a selector. This is
   what keeps adapters small, and it is why the brief's `detect/extract/fill/validate/submit/
   recover` interface should carry canonical field IDs, not raw selectors.
3. **Cost control belongs in the funnel, not the prompt.** Gate with deterministic scoring,
   batch what survives, cache aggressively.
4. **Browser automation cannot be tested against live sites.** neonwatty's
   record → scrub → compile → replay pipeline with a submit tripwire is the only credible CI
   story in the corpus.
