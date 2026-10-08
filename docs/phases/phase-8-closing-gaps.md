# Phase 8 — Closing the gaps

**Status: complete for everything that did not need you.** 1,480 backend tests · 15
frontend tests · ruff, mypy, eslint and tsc clean · 31 tables · 10 frontend routes.

Not a planned phase. It came from asking *"what's remaining?"* and getting an answer whose
headline was not a code gap: **the system had never been run to an outcome.** Zero
applications submitted, zero messages sent, zero facts verified, zero companies researched.

Running it found four real bugs, three of which only appear when the system is actually
used.

---

## The bugs

### The Fact Bank had no way to confirm a fact

`Fact.verified` has carried the same comment since the first migration — *"flipped by human
review, never by the parser; unverified facts are usable for matching but NOT for generated
resume claims."*

There was no human review anywhere in the system. No endpoint set the flag, no command set
it, and `GET /candidate/facts` did not even return an `id`, so nothing could address a fact
in order to confirm one. **0 of 49 verified was not neglect — it was unreachable.**

Now: `POST /candidate/facts/verify` takes an explicit list of ids, and a `/facts` screen
built for getting through forty-nine of them without it feeling like a chore — keyboard
first, evidence locator on every row so the claim can be checked against the page it came
from.

There is deliberately **no "verify all"**, on the endpoint or the screen. This flag is what
lets a claim reach a resume sent to an employer; one click that sets it across the table
would make the Fact Bank ceremonial.

### The database locked itself out

Found by doing the ordinary thing: starting a company research pass, then clicking a
checkbox.

```
sqlite3.OperationalError: database is locked
```

Two independent causes stacked.

**A transaction around the whole crawl.** `careeragent companies --research` wrapped
sixty-six website reads in one `session_scope()` — a single write lock held for minutes,
*across HTTP calls*. Every other process sharing the database got an error for the duration.
It was also all-or-nothing, which was demonstrated rather than theorised: interrupting the
run rolled back every profile it had gathered, and the researched count went back to zero.

**SQLite's defaults.** A rollback journal, where a writer excludes every reader, and
`busy_timeout` at zero, so a blocked connection fails instantly rather than waiting.

Fixed with one short transaction per company — the network read happens outside any
transaction now — plus WAL, a ten-second busy timeout, and `PRAGMA foreign_keys=ON`.

The pragma order matters and was wrong first time: switching journal mode takes a brief
exclusive lock, so `busy_timeout` has to be set **before** it or the switch fails instantly
on a database somebody else is using. And the switch is non-fatal — refusing to start the
API because a background crawl is mid-write is the wrong failure.

### Foreign keys were never enforced

Turning them on immediately failed **7 tests** that were inserting applications pointing at
jobs that did not exist. The constraints had been in the schema all along and SQLite was
ignoring every one, so `ON DELETE CASCADE` was decoration and orphan rows went in silently.

Fixed in the test data, not by turning the pragma back off.

### Relevance was somebody else's

The Phase 5 item. `PersonRecord.relevance` is one slot on a shared row, so the last ranking
to run overwrote everybody else's — with two users, opening the People screen showed a 96
computed for another person's career against another person's target role.

Moved to `person_relevance`, keyed on (person, candidate), with the 50 existing scores
backfilled to the sole candidate rather than discarded. The old columns stay as a deprecated
mirror: every read path uses them, and moving storage and readers in one commit is how a
migration becomes an outage.

Two tests now assert that an engineering manager scores *differently* for an ML candidate
and a sales one, and that a person nobody ranked for you reads as `None` rather than 0.

---

## Also built

**Renders became addressable.** `resume_render.storage_key` held
`D:\Joby agent\data\storage\renders\x.pdf` — not a key despite the column name, and a value
that pins the database to one machine. Renders now go through the object store and the
column holds `renders/<uuid>.pdf`; readers resolve it through `materialise()`, which also
still accepts the legacy absolute paths in existing databases.

That change broke render *reuse* instantly, and the existing test caught it: the reuse check
was `Path(key).exists()`, true only while the value was a path. Nothing would have failed —
every application would just have quietly paid for a second strong-tier LLM rewrite and a
second PDF. The kind of regression that survives for months.

**The approval screen**, Phase 3's named gap. It was worse than "the screen is not there":
no endpoint could produce the six things a reviewer needs together, so approving from the
list view meant approving a subject line. `GET /outreach/review` assembles them — recipient
and their relevance with reasons, the **URL the address was found at**, the full message,
the attached resume, prior contact across every channel, and suppression state — and the
screen shows the message in full, never truncated.

A draft that cannot be sent says why and **disables the approve button**: a gate you pass
without seeing anything is not a gate. Verified live against a real queued draft — the
button was correctly disabled for a company-level draft with no recipient, and Cancel
worked end to end.

**Frontend tests**, Phase 7's named gap. 15 of them, on the two components that encode the
project's central rule, because a regression there is invisible — a bar that renders solid
when it should be hatched looks completely fine. They assert distinctions, not appearances:
not-measured ≠ measured-as-zero, thin evidence ≠ thick evidence, loading ≠ failed ≠ empty.

---

## Data, before and after

| | before | after |
|---|---|---|
| Companies researched | 0 of 66 | **53 of 66** |
| Per-candidate relevance rows | — | **50** |
| Facts verified | 0 of 49 | 0 of 49 — *now possible* |
| Tables | 30 | 31 |
| Backend tests | 1,475 | **1,480** |
| Frontend tests | 0 | **15** |

The 13 companies that did not research failed honestly: bad domain guesses
(`ConnectError`) and JavaScript-only sites (`no readable pages`). Nothing was invented to
fill the gap.

---

## Still yours, not mine

Four things cannot be done from here, and one should not be:

* **Adzuna keys** and a **Supabase project** need your signup.
* **Gmail consent** needs you to complete OAuth in a browser.
* **Apollo credits** reset on their own — every enrichment call in this run returned 422.
* **`careeragent account set-password`** prompts for a secret only you should choose.

And **verifying the 49 facts**. "Verified" means *a human confirmed this is true about me*,
and it is the flag that lets a claim reach an employer. Setting it on your behalf would make
the Fact Bank's central guarantee ceremonial and the no-fabrication rule decorative. The
screen at `/facts` exists so it takes about two minutes.

## Still remaining

**Campaigns** — a campaign object with per-campaign limits and target sets. Not started.

**The fuller status machine** — `Delivered`, `Follow-up Due`, `Interested`, `Not Interested`
as distinct states. Reply and bounce detection still need the `gmail.readonly` scope, which
is a much broader grant than `gmail.send` and remains your decision.

**Screenshots still write straight to disk.** Renders go through the object store now;
Playwright needs a real path during a live session, so moving those touches the runner's
lifecycle rather than one call site.

**Sending, submitting and uploading** remain on the old dashboard. Unchanged reasoning: a
second send button built during a migration is a second way for a message to go out by
accident.

**Nothing has still been submitted or sent**, so six of the seven named insight queries
remain unanswerable, and the `INTERVIEW` / `OFFER` / `RESUME_VERSION` graph nodes have still
never been drawn.
