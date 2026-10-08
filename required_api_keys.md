# Required API keys and gateways

Everything this project can use, what each one actually buys, and how to get it — **all on
free tiers**. No credit card is needed for anything marked *Free forever*.

Paste what you obtain into `.env` at the project root. That file is gitignored; nothing
here is ever committed, logged, or returned by any API in this project. The dashboard's
**Keys** page shows only the last four characters of each key.

---

## The short version

If you only do three things, do these:

| # | Service | Time | Why it matters most |
|---|---------|------|---------------------|
| 1 | **Gemini** | 2 min | The only genuinely required key. Nothing that reads or writes language works without an LLM. |
| 2 | **Adzuna** | 5 min | The only source with proper Indian **city** coverage. Measured: every other free source returns remote/US-centric results for Bangalore. |
| 3 | **JSearch** | 1 min | You already have the RapidAPI key — it just needs subscribing to the free plan. It currently returns 404 on every call. |

Everything else is genuinely optional and the system degrades gracefully without it.

---

## 1. Required

### Gemini — the language model

**Without it:** no resume tailoring, no job extraction, no answers, no cover letters, no
company summaries. Deterministic scoring still runs, so discovery and ranking work, but the
system cannot write anything.

```
GEMINI_API_KEY=...
```

* Get it: <https://aistudio.google.com/apikey>
* Free tier: 15 requests/minute, 1,500 requests/day on Flash — ample for this workload
* Free forever, no card

---

## 2. Job discovery — the corpus

The system reads company ATS boards directly and needs **no key at all** for that; it is
currently holding 1,748 jobs from 24 boards that way. These keys widen the net beyond
companies already known.

### Adzuna — **highest value key you do not yet have**

**Without it:** regional coverage is poor. Measured on this project — the keyless sources
(TheMuse, Remotive, Arbeitnow) are remote- and US-centric, and a Bangalore search returns
Lemon.io and A.Team rather than Indian employers.

```
CAREERAGENT_ADZUNA_APP_ID=...
CAREERAGENT_ADZUNA_APP_KEY=...
```

* Get it: <https://developer.adzuna.com/signup>
* Free tier: 250 calls/day, ~20 countries including proper India city support
* Free forever, no card. Approval is instant.

### JSearch via RapidAPI — Google Jobs index

**You already have this key.** It returns HTTP 404 on every call because the key is not
subscribed to the JSearch API specifically — on RapidAPI, a key and a subscription are two
different things.

```
CAREERAGENT_RAPIDAPI_KEY=...          # already in your .env
```

* Subscribe: <https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch> → **Basic ($0)**
* Free tier: 200 requests/month — small, so the router treats it as a scarce resource
* Free, no card for the Basic plan

### Jooble — breadth

Already configured and working. No published monthly cap. Note it resolves to **country**
level: "Bangalore, India" and plain "India" return identical results.

```
CAREERAGENT_JOOBLE_KEY=...            # already in your .env
```

* Get it: <https://jooble.org/api/about> (request form, key by email)

### Keyless sources — already working, nothing to do

TheMuse, Remotive and Arbeitnow need no key and are already in use. They cover remote roles
well and cities poorly.

---

## 3. Company discovery — finding employers

### Tavily — web search, **already working**

```
TAVILY_API_KEY=...                    # already in your .env
```

* Get it: <https://tavily.com>
* Free tier: **1,000 credits/month** — the larger budget, so it is asked first
* Free, no card

### SerpAPI — Google's actual results, **already working**

```
SERPAPI_API_KEY=...                   # already in your .env
```

* Get it: <https://serpapi.com>
* Free tier: **250 searches/month** — small, so it is the fallback, not the default
* Free, no card

### Apify — crawling pages that have no structured feed

```
APIFY_API_TOKEN=...                   # already in your .env
```

* Get it: <https://apify.com>
* Free tier: **$5 of platform credit per month**, renewing
* Free, no card

---

## 4. People and company intelligence

### Apollo — company facts, **already working but currently spent**

**Important correction to expectations:** Apollo's free plan **refuses people search**
(`mixed_people/search` returns 403 `API_INACCESSIBLE`). It cannot do contact discovery at
this tier, and no amount of code changes that. What it *does* give free is valuable:
company enrichment (industry, technology stack, headcount by department) and job postings
keyed on a domain.

```
APOLLO_API_KEY=...                    # already in your .env
```

* Get it: <https://apollo.io> → Settings → Integrations → API
* Free tier: limited monthly credits; **currently exhausted** and resets with the billing cycle
* Rate limits it publishes: 600/day, 200/hour, 50/minute

### Hunter.io — published company email addresses *(recommended addition)*

The compliant way to find professional contact details: it indexes addresses that companies
have **published**, and reports a verification status for each.

```
HUNTER_API_KEY=...
```

* Get it: <https://hunter.io/api-keys>
* Free tier: 25 searches + 50 verifications/month
* Free, no card

### Vibe Prospecting — *currently unreachable*

```
VIBE_PROSPECTING_API_KEY=...          # already in your .env
```

`api.vibe.co` does not resolve and `api.vibeprospecting.com` is unreachable. **Send me the
API documentation URL** and I will wire it; the provider slot exists and is unused.

### Clay — *not configured, paid*

Clay has no meaningful free API tier. The system treats it as optional; nothing depends on
it, and its absence never turns "no enrichment" into "score of zero".

---

## 5. Outreach

### Google / Gmail — sending from your own mailbox

**Already configured; needs your one-time consent.** The scope requested is `gmail.send`
**only** — it cannot read, search or delete anything in your mailbox.

```
GOOGLE_CLIENT_ID=...                  # already in your .env
GOOGLE_CLIENT_SECRET=...
GOOGLE_PROJECT_ID=...
GOOGLE_REDIRECT_URI=http://localhost:8000/api/v1/outreach/gmail/callback
```

* Console: <https://console.cloud.google.com> → APIs & Services → Credentials
* Free tier: Gmail API is free; sending limits are your own account's
* **Action needed:** open the Outreach page → **Connect Gmail** → approve once

> Reply detection would need `gmail.readonly`, a much broader scope. It is deliberately not
> requested. Say the word if you want it and I will add it as a separate opt-in.

### LinkedIn — **external limitation, not a missing key**

LinkedIn's terms prohibit automated access, and no free API grants messaging. The system
will not submit credentials or bypass bot checks. The supported path is assisted: you log
in in a visible browser, the agent prepares the message, you send it. Nothing is ever
recorded as sent without confirmation.

---

## 6. Additional language models — optional fallbacks

The router uses whichever are configured and falls back when one is rate-limited.

| Service | Variable | Free tier | Get it |
|---|---|---|---|
| **Groq** | `GROQ_API_KEY` | Generous free tier, very fast | <https://console.groq.com/keys> |
| **OpenAI** | `OPENAI_API_KEY` | Pay-as-you-go, no free tier | <https://platform.openai.com/api-keys> |
| **Anthropic** | `ANTHROPIC_API_KEY` | Pay-as-you-go, no free tier | <https://console.anthropic.com> |
| **xAI / Grok** | `XAI_API_KEY` | Your key works but the team has **no credits** | <https://console.x.ai> |
| **Ollama** | *(none)* | Free, local, no key — install and run | <https://ollama.com> |

Groq is the best free addition here. Ollama costs nothing at all if you have the RAM.

---

## 7. Infrastructure — all optional, all free

| Service | Variable | Why | Free tier |
|---|---|---|---|
| **Supabase** | `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY` | Postgres + auth + file storage, for multi-user | 2 projects, 500 MB DB, 1 GB storage — <https://supabase.com> |
| **Upstash Redis** | `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN` | Queues, rate limits, caching | 10,000 commands/day — <https://upstash.com> |
| **LangSmith** | `LANGCHAIN_API_KEY`, `LANGCHAIN_TRACING_V2=true` | See why an agent decided something | 5,000 traces/month — <https://smith.langchain.com> |

Without these the system runs on SQLite, in-process asyncio and structured logs — which is
what it does today, and what every measurement in the docs was taken on.

**Redis** (`CAREERAGENT_REDIS_ENABLED=true` plus a URL) shares rate-limit buckets across
processes. Off, limits are per-process — correct for one worker, and `/api/v1/status` reports
which backend is in use rather than implying. Redis is *not* used for queues; see
[phase-6-infrastructure.md](docs/phases/phase-6-infrastructure.md) for why.

**Supabase storage** needs `CAREERAGENT_SUPABASE_URL` **and**
`CAREERAGENT_SUPABASE_SERVICE_KEY`. Both or neither: a URL with no key produces 401s at the
moment somebody saves a resume. This path has never been run against a real project — it is
written and typed, and it is not verified.

**LangSmith** draws the shape of a run. Without a key it is inert and says so. The durable
audit trail — `llm_call`, `provider_call`, `agent_decision` — does not depend on it.

### A warning about this file's format

`.env` has two kinds of line and they are **not** interchangeable:

```bash
TAVILY_API_KEY=                  # a credential. Blank is always safe.
# CAREERAGENT_AUTONOMY_LEVEL=4   # a setting with a default. Commented out on purpose.
```

`KEY=` is read as the empty **string**, not as "unset". Under an int, float, bool or Literal
setting that fails validation and the application will not start. Uncomment a setting only
when you are changing it, and never put a comment on the same line as a value — everything
after `=` is the value, `#` included.

### Accounts and sessions

Identity does **not** need any key. It is off by default, and switching it on needs one
value you generate yourself rather than obtain from anybody:

```bash
careeragent account secret        # prints CAREERAGENT_AUTH_SECRET=... for your .env
```

| Variable | Needed when | Notes |
|---|---|---|
| `CAREERAGENT_MULTI_USER` | you want logins enforced | `false` by default; one person on one machine needs nothing |
| `CAREERAGENT_AUTH_SECRET` | `MULTI_USER=true` | signs session tokens. **Refused if unset** — a built-in default would be a published key |
| `CAREERAGENT_AUTH_TOKEN_TTL_HOURS` | optional | 12 by default |
| `CAREERAGENT_SUPABASE_JWT_SECRET` | only for Supabase sign-in | Supabase issues HS256 tokens; setting this lets the same verifier accept one |

The secret is generated locally and stored in `.env`, which is gitignored. It is never
logged and never returned by any endpoint.

After turning multi-user on for the first time, set a password on the account the tenancy
migration created for your existing profile — it deliberately has none:

```bash
careeragent account list
careeragent account set-password you@example.com
```

---

## 8. The environment declaration — no signup, but **set it**

Not a key. It decides whether this process may destroy its own database, and it is the
guard that exists because a test once dropped a real one.

```
CAREERAGENT_ENV=development
```

* `production` — refuses every destructive operation, even forced
* `staging` — same, for a real-data test environment
* `development` — the default; refuses unforced drops, allows `careeragent init --reset` after it asks you
* `test` — the only value under which tables may be dropped freely

---

## What to paste into `.env`

Everything below, with the values filled in. Lines you have not obtained can stay empty —
the system checks each one and degrades rather than failing.

```dotenv
# ── Environment ──────────────────────────────────────────────────────────
CAREERAGENT_ENV=development

# ── Required ─────────────────────────────────────────────────────────────
GEMINI_API_KEY=

# ── Job discovery ────────────────────────────────────────────────────────
CAREERAGENT_ADZUNA_APP_ID=
CAREERAGENT_ADZUNA_APP_KEY=
CAREERAGENT_RAPIDAPI_KEY=
CAREERAGENT_JOOBLE_KEY=

# ── Company discovery ────────────────────────────────────────────────────
TAVILY_API_KEY=
SERPAPI_API_KEY=
APIFY_API_TOKEN=

# ── People and company intelligence ──────────────────────────────────────
APOLLO_API_KEY=
HUNTER_API_KEY=
VIBE_PROSPECTING_API_KEY=

# ── Outreach ─────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_PROJECT_ID=
GOOGLE_REDIRECT_URI=http://localhost:8000/api/v1/outreach/gmail/callback

# ── Extra language models (optional) ─────────────────────────────────────
GROQ_API_KEY=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
XAI_API_KEY=

# ── Accounts (optional; needed only with CAREERAGENT_MULTI_USER=true) ────
# Generate with: careeragent account secret
CAREERAGENT_MULTI_USER=false
CAREERAGENT_AUTH_SECRET=
CAREERAGENT_AUTH_TOKEN_TTL_HOURS=12
CAREERAGENT_SUPABASE_JWT_SECRET=

# ── Infrastructure (optional) ────────────────────────────────────────────
SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_KEY=
UPSTASH_REDIS_REST_URL=
UPSTASH_REDIS_REST_TOKEN=
LANGCHAIN_API_KEY=
LANGCHAIN_TRACING_V2=true
```

---

## Handling

* `.env` is gitignored (`.env`, `.env.*`, with `!.env.example`). Keys never reach the repo.
* No key is ever written to a log line or returned by an API. Responses carry a four-character
  fingerprint — enough to tell two keys apart, useless to anyone else.
* **Rotate anything you have pasted into a chat**, including the keys already in your `.env`
  from earlier in this project.
* The **Keys** page in the dashboard shows live status and remaining quota for every service,
  and runs a real health check rather than trusting that credentials exist — Apollo reports
  `{"healthy": true}` for an account with zero credits, which is exactly the trap it avoids.

---

## What is genuinely blocked, and by whom

| Item | Nature | Who unblocks |
|---|---|---|
| Adzuna keys | **User action** — free signup | You, 5 minutes |
| JSearch 404 | **User action** — subscribe existing key | You, 1 minute |
| Apollo credits | **Provider limit** — exhausted this cycle | Resets automatically |
| Apollo people search | **Provider limit** — not in the free plan | Paid tier, or use Hunter instead |
| Gmail sending | **User action** — one-time consent | You, 1 minute |
| Vibe Prospecting | **Provider limit** — host does not resolve | Send me the docs URL |
| LinkedIn automation | **Platform limitation** — terms prohibit it | Nobody; assisted workflow is the compliant path |
