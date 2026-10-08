# Phase 7 — Next.js frontend

**Status: complete for the read-heavy half, with the irreversible flows deliberately left
where they are.** Next.js 16.3.5 · React 19.2 · Tailwind 4 · TypeScript strict · build
clean · eslint clean · `tsc --noEmit` clean · 8 routes.

The index called this "a migration rather than a rescue — which is why it is last". That
framing turned out to be right and also to be the reason the scope is what it is: the old
dashboard is complete and works, so there was no pressure to reproduce it badly. What this
phase set out to do instead was give the system an interface that argues for the thing the
system is actually good at.

---

## The one idea

**Nothing on screen may look more certain than the data behind it.**

The backend never reports a bare score. It reports a score *and* how much of it rests on
evidence — because the same bug was found three separate times: an unmeasured component
contributing a zero, and nobody able to tell "bad" from "not measured". Every dashboard
that renders that as one solid bar throws away the distinction the backend paid for.

So `<Measure>` is the component the whole interface is organised around, and it draws
three regions rather than one:

```
████████████▨▨▨▨▨░░░░░░░
measured     unmeasured  headroom
```

The hatched band is the part nobody could measure. It is drawn rather than omitted,
because an empty track reads as zero and a shorter track reads as certainty.

The same rule runs everywhere:

| | |
|---|---|
| `<Percent>` | an em dash for `null`, never `0%` — they mean opposite things |
| `<Panel>` | **loading**, **failed** and **genuinely empty** are three different states, and the empty one says *why* |
| Insights | a rate below five submissions is never a percentage |
| People | **contactable** ≠ **reachable** ≠ **no channel** |
| Graph | a capped view says it is capped |

The People screen is where this matters most. Zero of the 91 discovered people have a
published email and none was guessed — a single green "contactable" tick would hide that
entirely, so the three states are separate badges and the count of published addresses is
on the header strip in its own right.

---

## What was built

Eight routes, grouped in the rail by *what you are doing* rather than by which table the
data lives in. A job hunt has phases; a nav that mirrors the schema makes you translate
every time you look at it.

**Command** — arranged around *what should I do next?*. Next moves (people and employers
with an opening you match who have heard nothing) sits above the counts, because almost
every other number on the page is retrospective. The funnel earns its place by being the
only view that shows where the hunt **stops** — the first stage with a zero under a
non-zero gets a marker.

**Openings / Companies** — filtered in the browser. A few hundred scored rows is nothing
to filter locally and a round trip to filter remotely, and a filter that responds on the
keystroke is a different tool from one that responds in 300ms.

**People** — relevance drawn with its confidence, expandable to the reasons behind the
score, and the three contact states above.

**Graph** — below.

**Insights** — the seven named questions from Phase 4, including the two whose whole point
is refusing to quote a rate.

**Keys** — providers by four-character fingerprint. A key is never displayed and there is
no field on the page that could hold one.

**Outreach** — read-only, on purpose. See "what is deliberately not here".

### The graph

Hand-rolled canvas, no library — the layout is a hundred lines of arithmetic and a graph
library is a megabyte of someone else's opinions about how you hold a mouse. Links pull,
every pair repels, a weak pull to centre keeps disconnected pieces on screen, and it cools
to a stop rather than shivering forever.

Two things a default force layout does not do:

**Hover focuses.** Everything that is not the hovered node or one of its neighbours dims to
near-nothing. At 118 nodes and 252 links that is the difference between a picture and a
hairball — hovering `pytorch` leaves exactly four things lit: you, and the three roles that
ask for it.

**Node colour is kind, radius is degree, and edge length is the backend's own `weight`** —
so a verified skill sits closer to you than an inferred one, and belief is visible as
distance rather than as a number nobody reads.

### ⌘K

Searches *across* things rather than within one. "Karya" is a company, two openings and six
people, and having to decide which you meant before you can type it is the friction this
removes.

Deliberately **not** a fuzzy matcher. Substring, case-insensitive, ranked by where the match
lands. Fuzzy matching is a delight on a fixed command list and a menace on live data — it
finds *something* for every query, so a typo returns a confident wrong answer rather than
nothing.

---

## Bugs found while building

**A stale backend.** The API on `:8000` had been running since before Phase 4 — no
`/graph/questions/*`, no `infrastructure` block. Two panels rendered as empty and correct,
which is exactly the failure mode Phase 6 was about: an empty result that looks like a true
one. Restarting it filled both.

**`w-full` on the measurement bar ate the labels.** `<Measure>` set `w-full` on its root,
which fought the `w-20` its callers passed. In a wide column nothing showed; in the narrow
Insights column the bar won the cascade and squeezed the company name to zero characters.
The names were in the DOM the whole time — they were simply nowhere. The fix is that the
caller owns the width, plus `shrink-0`, because a bar that can be compressed is a
measurement whose size depends on how long the label beside it is.

**Three requests asked for more rows than the API allows.** `/jobs?limit=400` is a 422;
the cap is 200. It surfaced as a permanent loading skeleton, because a failed fetch left
the state at `null` forever.

**Two React 19 correctness errors**, both real rather than stylistic: a `requestAnimationFrame`
callback that named itself to schedule the next frame (which also keeps every frame's
closure alive — a slow leak on a page left open all day), and a `lastGroup` variable
reassigned during render to decide section headings, which breaks the moment React renders
a component twice, and breaks by silently dropping headings.

One rule is disabled, for one function, with the reason written next to it: the physics
integrator mutates bodies held in a ref, and the alternative is allocating ~120 objects
sixty times a second to draw an identical picture.

---

## What is deliberately not here

**Sending. Approving. Submitting. Uploading.** Every flow whose last step cannot be undone
still lives on the original dashboard at `:8000`, and the Outreach screen links to it
rather than reimplementing it.

This is not a shortcut. A second send button, built quickly during a visual migration, is a
second way for a message to go out by accident — and the whole outreach design is three
suppression checks, cross-channel duplicate protection and an approval that cannot be
skipped. Those move when they can move carefully.

**The old dashboard is not deleted, and should not be.** It is complete, it works offline,
and it is the fallback while this front end covers only part of the surface.

---

## Verified against live data

Every screen was checked in a real browser against the running backend — 1,748 openings, 24
boards, 91 people across 8 companies, 49 facts, 10 configured providers. Not a fixture in
sight.

| Screen | What it showed |
|---|---|
| Command | 1,748 / 24 / **0 of 49 facts verified** in amber; three Karya people as next moves |
| Openings | 200 rows, scores 81% → 48%, missing-skill hints, remote pills |
| People | 91 discovered · 29 worth contacting · **0 published emails** |
| Graph | 118 nodes, 252 links; hover on `pytorch` lit exactly its four neighbours |
| Insights | seven panels, all answering |
| Keys | 10 configured, 3 unset, fingerprints only |
| ⌘K | "karya" → 2 openings + 6 people, grouped |

---

## Running it

```bash
job/Scripts/python.exe -m uvicorn careeragent.api:app --port 8000 --app-dir backend
npm --prefix frontend run dev        # http://localhost:3000
```

`NEXT_PUBLIC_API_BASE` overrides the API location. CORS already allows `localhost:3000`.

---

## What remains

**The irreversible flows**, above — the largest remaining piece, and the one that should be
done slowly.

**No tests.** The backend has 1,475; this has none. A component test for `<Measure>` and
`<Panel>` would be worth having precisely because they encode the project's central rule,
and a regression there would be invisible — a bar that renders solid when it should be
hatched looks fine.

**Single-user only.** The API client reads a bearer token from `localStorage` and attaches
it, but there is no login screen here; multi-user mode currently means using the old
dashboard, which has one.

**Not tested below ~900px.** The rail hides under `md` and the layouts are responsive by
construction, but nothing has been checked on a phone.
