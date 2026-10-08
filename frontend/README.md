# Career Agent — web

The Next.js front end. It talks to the FastAPI backend and holds no state of its own.

```bash
# the API first — the UI is useless without it
job/Scripts/python.exe -m uvicorn careeragent.api:app --port 8000 --app-dir backend

# then, from this directory
npm run dev        # http://localhost:3000
```

`NEXT_PUBLIC_API_BASE` overrides the API location; it defaults to
`http://127.0.0.1:8000/api/v1`. CORS on the backend already allows `localhost:3000`.

---

## The one idea

**Nothing here may look more certain than the data behind it.**

The backend never reports a bare score. It reports a score *and* how much of it rests on
evidence, because the same bug was found three separate times: an unmeasured component
contributing a zero, and nobody able to tell "bad" from "not measured". A dashboard that
renders that as one solid bar throws away the distinction the backend paid for.

So `<Measure>` draws three regions and all of them are visible:

```
████████████▨▨▨▨▨░░░░░░
measured    unmeasured  headroom
```

The hatching is the part nobody could measure. It is drawn rather than omitted — an empty
track reads as zero, and a shorter track reads as certainty.

The same rule runs through everything else:

- `<Percent>` prints an em dash, never `0%`, when a value is `null`.
- `<Panel>` distinguishes **loading**, **failed** and **genuinely empty**, and an empty
  panel says *why* it is empty. Collapsing those three into a blank rectangle is how a
  backend that is down comes to look like a job hunt that has not started.
- Rates below five submissions are not shown as percentages anywhere, because one response
  from one application is not a 100% response rate.
- The People screen separates **contactable** (a verified published address — the system
  may send) from **reachable** (a profile you open yourself) from **no channel**. Zero of
  the 91 people in the corpus have a published email and none was guessed; a single green
  tick would hide that.

## Layout

```
src/
  app/
    page.tsx          Command — what to do next, the funnel, the machinery
    jobs/             Openings, filtered in the browser
    companies/        Employers rolled up from the openings
    people/           Who to write to, and by what channel
    outreach/         Drafts, sends, do-not-contact (read-only — see below)
    graph/            The canvas
    insights/         The seven named questions
    keys/             Providers, by fingerprint
  components/
    Shell.tsx         Rail, status strip, ⌘K
    CommandPalette.tsx
    Measure.tsx       Measure, Ticker, Percent
    Panel.tsx         Panel, Stat, Pill
  lib/api.ts          Every call, and the types, read off live responses
```

Navigation is grouped by *what you are doing* — find work, reach people, understand — not
by which table the data lives in. A job hunt has phases, and a nav that mirrors the schema
makes you translate every time you look at it.

## The graph

Hand-rolled canvas, no library. Links pull, every pair repels, a weak pull to centre keeps
disconnected pieces on screen, and the whole thing cools to a stop rather than shivering
forever.

Two things it does that a default force layout does not:

- **Hover focuses.** Everything that is not the hovered node or one of its neighbours dims
  to near-nothing. At 118 nodes that is the difference between a picture and a hairball.
- **The soft wall.** Repulsion pushes low-degree nodes outward; clamping them at the edge
  parked them in a neat row that looked like a deliberate layout. A force that grows as it
  nears the boundary turns them back instead.

Node colour is kind, radius is degree, and edge length is the backend's own `weight` — so a
verified skill sits closer to you than an inferred one, and belief is visible as distance
rather than as a number nobody reads.

## What is deliberately not here

**Sending.** Drafting, approving and sending outreach still live on the original dashboard
at `:8000`. Those flows end in an irreversible action, and a second send button built
quickly during a visual migration is a second way for a message to go out by accident. They
move when they can move carefully.

Same for resume upload, application preparation and approval, and settings — every screen
whose last step cannot be undone. The old dashboard is complete and still works; this front
end covers the read-heavy half, which is where the design mattered most.

## Verified against

The live corpus: 1,748 openings, 24 boards, 91 people across 8 companies, 49 facts, 10
configured providers. Every screenshot in the phase doc is real data, not a fixture.
