# Phase 1 — People foundation

**Status: complete.** Tests at end of phase: 1,256 passing.

The goal was to make company people intelligence real rather than a set of types: storage,
an API, a screen, and a rule about contact details that cannot be forgotten.

---

## What was built

### The no-fabrication rule, made structural

`backend/careeragent/models/person.py`

`ContactDetail` has no constructor that omits provenance. This raises:

```python
ContactDetail(value="guess@acme.com", kind=SourceKind.TEAM_PAGE, source_url="")
# ValueError: a team_page contact detail needs the URL it was found at —
# a detail with no source cannot be distinguished from a guess
```

There is nowhere in the codebase to put an email address nobody published. The temptation
this forecloses is specific: given "Rahul Kumar" at `acme.com`, `rahul.kumar@acme.com` is a
plausible guess, and a plausible guess sitting beside real data is indistinguishable from
it. Guessing is *not implemented* rather than discouraged, because a discouraged thing
eventually gets implemented.

Four verification states — `VERIFIED`, `UNVERIFIED`, `INFERRED`, `UNKNOWN`. Only `VERIFIED`
is ever sent to without asking again. `INFERRED` exists to be refused: nothing in this
project produces one, and the state is there so a provider that admits to guessing can be
labelled honestly and then declined.

### Career-aware relevance

`backend/careeragent/people/relevance.py`

Seven components — function, authority, proximity, seniority, technical relevance,
reachability, contact history. Each can be unknown, and an unknown one surrenders its
weight rather than scoring zero.

The design point is that **a CEO is not automatically the best contact**:

| Person | 3,000-person company | 12-person startup |
|---|---|---|
| AI Engineering Manager | **96/100** | 96/100 |
| Head of Data | 84/100 | 84/100 |
| VP Engineering | 69/100 | 69/100 |
| Technical Recruiter | 67/100 | 67/100 |
| Chief Executive Officer | **12/100** | **48/100** |
| Finance Manager | 8/100 | 8/100 |

The 96 and the 8 match the specification's own worked example. The CEO moving 12 → 48 with
company size is what stops this being a seniority ladder: at twelve people the founder *is*
the hiring manager; at three thousand they will not read the message and could not act on
it.

Authority is scored **over the function being targeted**. A finance manager has all the
seniority of an engineering manager and none of the ability to hire an ML engineer. Without
that gate they scored 45/100.

### Entity resolution

A LinkedIn URL or a work email is identity. A matching name alone is never enough — two
people called Rahul Kumar can work at the same company, and a wrong merge means a message
addressed to one describing the other's job. Merging keeps the better-sourced detail:
a company's own page beats an enrichment provider on the same fact, because the company is
the authority on who works there.

### Storage

`backend/careeragent/people/store.py`, migration `fb250cab12e4` (27 tables)

`PersonRecord` and `PersonSource`. Contact details store their source, verification state
and confidence as columns rather than as bare strings — there is nowhere in the schema to
put an address with no provenance either.

Relevance is stored as the *last computed* value with the job it was computed for, because
relevance is not a property of a person: the same engineering manager is a 96 for an ML
candidate and an 8 for someone applying to finance.

### API and screen

* `POST /companies/{id}/people` — discover, rank, persist
* `GET /companies/{id}/people`
* `GET /people` with every filter from the spec, `GET /people/functions`
* A **People** page where relevance leads, every address shows its verification and links to
  the page it came from, and anything unverified says *not sent automatically*

---

## Bugs found while building

**The target role was always "Engineer".** The fallback read a `job_title` fact that the
resume parser never produces. It now asks the career detector, which already knew the
answer: **"Machine Learning Engineer"**.

**A weaker later sighting could downgrade a verified address.** A thinner crawl would have
silently destroyed a good record. Verification now wins over recency.

**`row.sources` on a freshly-flushed row raised `MissingGreenlet`** — a lazy load inside an
async session. This would have hit production, because the route calls `save` the same way.
Source URLs are now fetched with an explicit query.

---

## What this phase did not solve

Discovery found **2 people across 8 companies**. Correct — zero fabrications — but nearly
useless. That is Phase 2.
