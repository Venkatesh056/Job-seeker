# Phase 6 — Infrastructure

**Status: complete, with one item deliberately not built.** Tests at end of phase:
**1,475 passing** (up from 1,429). Ruff clean · mypy clean across 154 source files.

Planned as "Redis queues and rate limits, LangSmith tracing, Supabase storage, a systematic
failure-injection suite". Four of those were worth building. The fifth — queues — was not,
and the reasoning is in this document rather than in a half-built module.

The failure-injection suite was expected to be the boring one. It found two real bugs
within an hour, one of which was a path traversal.

---

## What was built

### Outbound rate limiting — `backend/careeragent/net/limits.py`

A token bucket per provider, in front of the highest-volume path in the system. 24 ATS
boards, several search providers and a handful of enrichment APIs, all hit in bursts by an
agent that does not get tired. Being impolite here does not produce an error — it produces a
429, then a block, and the block arrives days later as *"that board stopped working"*.

A bucket rather than a fixed window: a fixed window lets a caller spend its whole allowance
in the first millisecond and then crowd the boundary, which is twice the nominal rate
arriving as two bursts.

Limits are **per provider, not one global number**, because what they protect against
differs in kind. The ATS endpoints belong to companies who did not ask to be crawled;
the search providers sell credits and publish a rate. A company's own marketing site gets
the slowest setting of all — there is no plan under which we are entitled to its capacity.

Two ways to be limited, because they are not the same need:

* `acquire()` **waits** — for politeness; the work is still worth doing
* `try_acquire()` **refuses** — for hard caps, where waiting means an agent parked on a
  provider whose budget is already spent

Three decisions worth stating:

**The limiter runs inside the retry, not outside it.** A 429 is retried by the decorator, so
a limiter that only ran on the first attempt would let the retries through unthrottled —
hitting hardest exactly when the server has said stop.

**`Retry-After` outranks our own number.** A limiter that ignores it is guessing against a
server that has stopped guessing. A 429 with no header still means slow down, so a missing
value becomes a default rather than zero.

**The memory backend says it is per-process.** A limiter that does not cross processes
quietly lies the moment a second worker starts — each politely obeying a limit, together
exceeding it. Which backend is in use is reported by `/api/v1/status`, not assumed. The
Redis backend does the read-modify-write in one Lua script, because two workers each finding
a token and each taking it is precisely the over-spend this exists to prevent, and it only
appears under load. **A Redis failure fails open** — a cache outage should not take down job
discovery.

### Object storage — `backend/careeragent/storage/objects.py`

Four places built their own path out of `settings.storage_dir`, which was fine while the
answer was always "a folder on this laptop". It stops being fine with a second one: a
screenshot is the *evidence* a submission happened, and evidence on a container's local disk
disappears on the next deploy.

One API, two backends, local by default. Writes go beside-then-rename, so a reader sees the
old bytes or the new ones and never half a PDF, and a crash leaves a `.partial` rather than a
truncated artifact.

**A key is not a path.** Callers name a logical object (`renders/<id>.pdf`) and the backend
decides where bytes live. `_safe_key` *refuses* anything with `..` or a leading slash rather
than normalising it — normalising a hostile path quietly produces a valid one.

### Tracing — `backend/careeragent/net/tracing.py`

Deliberately tiny. This system already records more than most: every LLM call in `llm_call`
with its prompt version and cost, every provider call in `provider_call`, every agent
decision in `agent_decision` with the evidence behind it. That is the audit trail, it is in
the database, and it survives a restart.

What it lacks is the *shape* of a run. LangSmith draws that, and LangChain reads its own
configuration from the environment, so this module sets three variables and reports honestly.
**Without a key it is a no-op and says `enabled: false`** — a tracing integration that
silently does nothing is worse than none, because it makes people stop looking for the reason
their traces are missing.

### Fault injection — `backend/careeragent/net/faults.py`

An `httpx` transport, not a mock of our own functions, and the distinction is the whole
point. Patching `ATSFetcher._request` would test the code above the patch and skip the retry
decorator, the rate limiter, the status handling and the JSON parsing — which is exactly the
layer where these bugs live. A transport sits underneath all of it, so the real client code
runs unmodified against a hostile server.

```python
async with fault_client({"*greenhouse*": Fault.html_error_page()}) as client:
    async with ATSFetcher(client) as fetcher:
        result = await fetcher.fetch_board(Greenhouse, "acme")
```

Thirteen failure modes, in three groups:

| Raise | Return | **Lie** |
|---|---|---|
| timeout | 429 with/without `Retry-After` | 200 + Cloudflare HTML |
| connect error | 4xx / 5xx | truncated JSON |
| connection reset | slow-but-correct | empty body |
| | | wrong shape |

The third column is the dangerous one. A timeout raises and somebody catches it. A truncated
JSON array produces **twelve jobs where there were forty**, no error anywhere, and a board
that silently looks like it stopped hiring.

An unmatched URL returns 599 rather than reaching the network. A fault suite that quietly
made real requests would be slow, flaky, and occasionally rude to somebody's server.

### `careeragent storage`

`usage` and `prune`. Nothing had ever deleted a screenshot — every application attempt leaves
several, and they accumulate with no bound. A prune nobody can run is not a retention policy,
it is a function.

`--keep` is a floor that age cannot override: *"delete everything older than 90 days"* on a
system nobody has used for 91 days deletes the only evidence it has. Resumes are never
pruned — a master resume is the input to everything else and is not regenerable from
anything here.

---

## Two bugs, both found by the new tests

### A board that repeated a page produced 42 jobs from 3 openings

```
ok=True   jobs=42   distinct ids=3   http requests=14
```

The paging condition was "keep going until the platform's own total is reached", which is
right, and it had no second condition for the platform being **wrong about its own total**, or
simply stuck. Nothing raised, because nothing was broken from the loop's point of view: it
asked for more, it got rows, it counted them.

Downstream, `upsert_jobs` deduplicates on a fingerprint, so the database would have survived.
The *reports* would not — `last_job_count`, `total_new_jobs_seen` and every run summary would
have said 42.

Two independent guards, because they catch different servers: a page that contributes nothing
new, and a `next_offset` that does not advance. After:

```
ok=True   partial=True   expected=40   shortfall=37
jobs=3    distinct=3     http requests=2
```

`partial` is kept separate from `error` on purpose. The rows that arrived are real openings
and throwing them away because forty were promised would be the worse mistake — but a caller
reporting "3 new jobs at Acme" off a partial fetch is reporting a number nobody can trust, so
the discrepancy travels with the result.

**Verified against the real APIs** that this did not break genuine pagination:
SmartRecruiters paged 138 jobs across multiple requests with `partial=False`, and Lever
returned 222.

### The resume upload could write anywhere on disk

```python
saved = target / (file.filename or "resume")
```

`file.filename` is chosen by whoever is uploading. Starlette does not sanitise it, and the
suffix allowlist above it does not help, because `../../../pwned.pdf` ends in `.pdf` and
passes every check the endpoint made. Measured:

```
../../../pwned.pdf   ->  D:\Joby agent\pwned.pdf         escapes: True
../../.env.pdf       ->  D:\Joby agent\data\.env.pdf     escapes: True
```

That second one is the interesting target.

The fix is **not a sanitiser**. The stored key is now generated (`resumes/<uuid>.pdf`) and the
client's name is kept only as display metadata, stripped to a bare basename that can never be
part of a path again. A sanitiser is something you have to keep getting right forever; a
generated key cannot be wrong.

Found because the object store already refused these keys, and integrating it meant looking
at what the endpoint was doing instead.

---

## What was not built, and why

**Redis queues.** Redis is integrated — it backs the rate limiter, which is a real
cross-process need with a real failure mode. Queues are not, and building them would have
been speculative complexity rather than infrastructure.

The system is a single-process asyncio application whose long work already survives restarts
through durable LangGraph checkpoints — an approval parked overnight resumes at the submit
step. There is no work being distributed across machines, no fan-out that outgrows a
semaphore, and no backlog that needs to survive independently of the process that made it.

What would change that: more than one worker, or a scheduled sweep heavy enough that the API
process should not be running it. Neither is true today, and a queue added before either is a
second place for work to be lost.

**Supabase storage is written but unexercised.** There is no Supabase project attached to
this deployment, so that code path has never made a real request. It is written, typed and
wired; it is not verified, and `describe()` says `"configured but never exercised"` so
nothing downstream can report it as working on the strength of it existing.

---

## Observable

`/api/v1/status` now carries an `infrastructure` block, because *"are my rate limits shared
across workers"* and *"where do screenshots live"* are questions whose wrong answer is
invisible until it matters:

```json
"infrastructure": {
  "rate_limits": {
    "backend": "memory (per-process; a second worker would not see these buckets)",
    "keys": 19
  },
  "object_store": "local (D:\\Joby agent\\data\\storage)",
  "tracing": {
    "enabled": false,
    "backend": "none",
    "durable_record": ["llm_call", "provider_call", "agent_decision", "agent_event"]
  },
  "multi_user": false
}
```

And on the live system:

```
Prefix         Objects      Size   Oldest
resumes              1  256.6 KB   2026-09-12
renders              2  143.0 KB   2026-09-14
applications        17  387.4 KB   2026-09-13
787.0 KB in local (D:\Joby agent\data\storage)
```

---

## Tests added

46 new, across two files.

`tests/test_failure_injection.py` — 22. An HTML error page is not parsed as jobs; malformed
JSON fails the board rather than returning an empty one (a board that *failed* is retried
later, a board that returned zero jobs is a company that stopped hiring); a wrong-shaped
response produces an error on one board rather than an exception across twenty-four; a
timeout is retried and bounded; a flaky board recovers without a human; a 5xx is retried and
a 404 is not; a 429 penalty is applied and a long one raises rather than hangs; a token is
never partially spent; `Retry-After` is parsed or defaulted but never zero by accident; and a
board that repeats a page is stopped — while a genuinely paginated one still pages.

`tests/test_object_store.py` — 24. Eight key shapes that would escape the store are refused
rather than sanitised; nothing partial is ever visible or listed; pruning keeps the newest
regardless of age; the store is local unless Supabase is *fully* configured (a URL with no
key produces 401s at the moment somebody saves a resume — the worst time to find a
half-configuration); tracing admits it is off; and four traversal filenames are driven
through the real upload endpoint.

---

## `.env`

Rebuilt with all 45 variables the code actually reads, every existing key preserved. Two
kinds of line, and they are not interchangeable:

```
TAVILY_API_KEY=                  a credential; blank is always safe
# CAREERAGENT_AUTONOMY_LEVEL=4   a setting with a default; commented out on purpose
```

`KEY=` is the empty **string**, not "unset" — under an int, float, bool or Literal setting it
fails validation and the application does not start. This was discovered by checking rather
than by reading, twice, which is why it is written down here.

---

## What remains

**The object store is integrated at one write path, not four.** Resume upload goes through
it. Renders and screenshots still write directly to disk, and `storage_key` still holds an
absolute path in those cases — which is also what the Gmail attachment path reads. Converting
them is a refactor of the download and attach endpoints, not an infrastructure task, and it
is the prerequisite for the Supabase backend being useful rather than merely present.

**Nothing calls `prune` on a schedule.** It is a command a person runs. Putting it on the
monitor tick is easy and was left out deliberately: automatic deletion of submission evidence
should be a decision somebody makes once, knowingly, not a default.
