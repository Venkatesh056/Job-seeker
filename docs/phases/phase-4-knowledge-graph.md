# Phase 4 — Knowledge graph completion

**Status: complete.** Tests at end of phase: **1,399 passing** (up from 1,383).
Ruff clean · mypy clean across 140 source files · 29 tables.

The graph already existed and already drew you, your skills, your jobs and their companies.
What it could not draw was anybody — and by this point the database held 91 real people,
their titles, their departments and every message ever sent to one of them. This phase
connected the two halves, and then asked the questions that only become askable once they
are connected.

One of those questions came back empty. That turned out to be the most valuable thing in
the phase.

---

## What was built

### Nodes and edges for the people half

`backend/careeragent/knowledge/model.py`

Eight new node kinds — `PERSON`, `ROLE`, `DEPARTMENT`, `OUTREACH`, `LEAD`, `INTERVIEW`,
`OFFER`, `RESUME_VERSION` — taking the graph to **18 kinds**, and ten new edge kinds to
**21**:

| Edge | Reads as |
|---|---|
| `HAS_PERSON` | company → person |
| `HAS_ROLE` | person → the title they hold |
| `WORKS_IN` | person → department |
| `RELEVANT_TO` | person → the opening they matter for |
| `TARGETS` | a message → the person it was written to |
| `REPLIED_TO` | that person → the message, when they answered |
| `ABOUT_JOB` | a message → the opening it was about |
| `QUALIFIED_AS` · `LED_TO` · `USED_RESUME` | outcomes, for when there are some |

`role` and `department` are separate nodes rather than attributes on the person because
they are what make the graph answer anything. Two people sharing a `role` node is the
structure behind "who else does this job here"; the same string sitting in two rows is not.

### The projection

`backend/careeragent/knowledge/project.py` — `_add_people()`, `_add_outreach()`

Capped at eight people per company, best first. A company with forty names on its
leadership page would bury its jobs, and the fortieth teaches nothing the first eight did
not — the relevance ranking has already decided which of them matter.

Drafts are excluded from the outreach projection. A draft is a decision the sender has not
finished making, and letting it into the graph would make "who have I contacted" answer
with people who have heard nothing.

Where a sent message's contact can be matched back to a discovered person, the graph shows
**one human** rather than a person node and a contact node side by side.

Projected live:

```
company:karya  →  32 nodes, 37 edges
{'company': 1, 'ats': 1, 'job': 12, 'person': 8, 'role': 8, 'department': 2}

   person      Mirza Aasim Baig     rel=84
   role        Lead AI Ops Engineer
   department  ai/ml
```

### Four more named questions

`backend/careeragent/knowledge/questions.py` — seven in total now.

**1 + 2. `companies_for_my_strongest_skills`** — companies whose open roles ask for what you
are strongest at, *with those roles attached*. The spec lists "which companies" and "which
openings" as two questions; they are one walk, and answering twice from it would let the two
answers drift apart. Ranked by how many of your strongest skills a company wants, not by how
many roles it has open: one role wanting four of your skills is a better lead than nine
wanting one. Ordinary English words are excluded from the matching for the same reason
`bridging_skills` excludes them — a company list built on "go" and "excel" is a list of
nothing.

**3. `untouched_companies`** — already existed.

**4. `uncontacted_at_matched_companies`** — people worth writing to at companies whose jobs
you match, who have not heard from you. Suppressed people are **dropped, not flagged**: a
row marked `suppressed: true` is still a row a client can render and somebody can click send
on. The count withheld is reported separately.

At most **three per company**. One well-crawled employer with thirty-nine staff would
otherwise fill the whole list, and "write to twelve people at Karya" is worse advice than
three each at four companies — twelve arrive as a campaign, which is precisely the thing
this system exists not to do.

`reachable` and *contactable* are kept apart. A LinkedIn profile means a person can be
reached by hand; only a verified published address means the system may send anything
itself.

**5. `applications_that_went_somewhere`** — what drew a response, and whether outreach
preceded it. A rejection counts: it is evidence the application was read, which is exactly
what silence is not.

**6. `resume_versions_by_outcome`** — which tailored resumes drew replies.

Both 5 and 6 **refuse to quote a rate below five submissions**:

```python
# Below this many submitted applications a "rate" is noise wearing a percentage sign.
# One response out of one application is not a 100% response rate, and reporting it as
# one would make the weakest evidence in the system look like the strongest.
_ENOUGH_TO_RATE = 5
```

An unrated version sorts *below* a rated one. A version with no rate is not a version with
a perfect one, and a list that puts it on top says otherwise to anyone reading quickly.

### Endpoints and screen

```
GET /api/v1/graph/questions/skill-companies
GET /api/v1/graph/questions/uncontacted-people
GET /api/v1/graph/questions/what-worked
GET /api/v1/graph/questions/resume-outcomes
```

All four verified 200 against the live database. Three new cards on the graph page, and
colours for the nine new node kinds — warm hues for the people nodes, because those are the
ones where a misclick costs something. `rate === null` renders as an em dash with the reason
in a tooltip, never as `0%`.

---

## The bug this phase found

**`job.company_id` was NULL on all 1,748 rows.**

The column and its foreign key had existed since the first migration. The only caller that
filled them was the per-company harvest; the broad searches, which produced almost the whole
corpus, do not know a company id and passed `None`. Nothing complained, because a nullable
column being null is not an error.

It stayed invisible for as long as nothing needed it. People hang off `company.id`. So the
first time anything asked *"who works at a company I have an opening with"*, the answer was:

```
note: No company has an open role you score above 45% on, so there is nobody
      to write to about one.
```

Which is a perfectly well-formed, perfectly honest, completely wrong answer — and
indistinguishable from the true one. **An empty result from a broken join looks exactly like
an empty result from an empty database.** This is the failure mode the phase existed to
expose: the people half and the jobs half of the system had been sitting side by side for
two phases without a single row joining them.

Two fixes, because there are two populations:

* **Forward** — `upsert_jobs` now resolves the company by normalized name when no id is
  passed, one lookup per distinct name rather than one per job.
* **Backward** — `repo.link_jobs_to_companies()` and `careeragent relink [--apply]` for the
  corpus already collected. It stays as a command rather than a one-off migration because
  the ordinary case recreates the state: research a company today and its jobs, harvested
  last week, are sitting there unlinked.

**Nothing is created to satisfy the join.** `company` means "a company we know something
about" — it is what the dashboard counts and what people, research and outreach hang off.
Inserting a bare row for every name in a search result would quietly turn 66 researched
companies into several hundred strings, and the number on the screen would stop meaning what
it says. A job naming an unknown company stays unlinked, which is the true state: we have
the job, we do not have the company.

Matching is normalized equality and nothing looser. A fuzzy match would attach one company's
people, research and outreach history to another with a similar name, and every answer built
on that join would be confidently wrong rather than visibly empty. `"Karya Labs"` does not
match `"Karya"`, and there is a test that says so.

Run against the live corpus, after a snapshot:

```
Linked 876 of 1748 unlinked job(s).
872 name no company in the database. Research them and run this again.
```

---

## Verified with real data

The same question, before and after:

| | Before | After |
|---|---|---|
| `uncontacted-people` | "nobody to write to" | 3 people at Karya, 2 with a channel |
| jobs joined to a company | 0 of 1,748 | 876 of 1,748 |

```
3 people across 1 companies with open roles you match, none of whom have heard from you.
2 have a channel already. At most 3 per company, best first.

  Mirza Aasim Baig      Lead AI Ops Engineer   Karya  rel=84  job=0.57  via=—
  Manikanteswar Punnam  Lead Backend Engineer  Karya  rel=77  job=0.57  via=linkedin
  Arvind Prakash        Head of Engineering    Karya  rel=74  job=0.57  via=linkedin
```

**One company, and that is the correct answer.** The other three employers with people in
the database fall short on one condition each, and the numbers say which:

| Company | People | Best open role | Why it is absent |
|---|---|---|---|
| Karya | 39 (max rel 84) | 0.57 | — |
| Clickpost | 2 (max rel 67) | **0.44** | just under the 0.45 threshold |
| Druva | 14 (max rel 53) | **0.36** | no role worth writing about |
| Browserstack | 1 (**max rel 12**) | 0.44 | nobody relevant, and no role either |

Relevance answers *"would this be the right person to ask"*. It says nothing about whether
there is anything to ask. Both have to be true, and only at Karya are they.

Skills → companies, live:

```
Glance     skills=5  best=0.81   deep learning, power bi, python, sql
   · Product Analyst II                              0.81
Weekday 1  skills=5  best=0.80   power bi, python, sem, sql
   · Agentic AI Engineer                             0.80
Quantiphi  skills=3  best=0.79   opencv, python, sql
```

---

## Tests added

16 new, in two files.

`tests/test_graph_questions.py` — twelve, each pinned to the thing that would make the
answer dangerous rather than merely wrong:

* a 90-relevance VP at a company whose only opening scores 0.10 is **not** a lead
* an opted-out person is **dropped**, and the withheld count is reported
* a sent message removes somebody; a **draft does not**
* one response from one application is **not** 100%
* a rate appears at five submissions and is real arithmetic (0.6 from 3 of 5)
* a rejection counts as a response but not as an interview
* an unrated resume version sorts **below** a rated one
* an empty table is a state, not a zero percent

`tests/test_company_links.py` — four on the join itself: the forward link, that no company
row is invented, that the backfill is a real dry run before it writes, and that `"Karya
Labs"` never matches `"Karya"`.

---

## Live data at the end of this phase

| | |
|---|---|
| Companies | 66 |
| Jobs | 1,748 · **876 now joined to a company row** |
| Jobs within 30 days | 735 |
| People | 91 across 8 companies · 29 worth contacting |
| — with a LinkedIn profile | 47 |
| — with a published email | **0**, and none guessed |
| Graph, candidate view | 119 nodes, 266 edges |
| Graph, `company:karya` | 32 nodes, 37 edges |

---

## What remains

Nothing in this phase. `INTERVIEW`, `OFFER` and `RESUME_VERSION` nodes exist and project
correctly, but there is nothing in the database to draw yet — no application has been
submitted. They will be exercised the first time one is, and questions 5 and 6 will report
counts without rates until there are five.
