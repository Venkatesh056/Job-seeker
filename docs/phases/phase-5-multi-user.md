# Phase 5 — Multi-user and authentication

**Status: complete.** Tests at end of phase: **1,429 passing** (up from 1,399).
Ruff clean · mypy clean across 149 source files · 30 tables.

The phase was described in the index as "the largest structural gap — `user_id` on every
entity, touches every table". It turned out not to be, and the reason is worth recording:
**the schema was never single-user.** `candidate_id` has been threaded through every
per-person table since the first migration. What made the system single-user was one line:

```python
row = (await session.execute(select(Candidate).limit(1))).scalar_one_or_none()
```

`limit(1)`. The first row in the table. There are **111 call sites** downstream of that
function across 23 files, and rewriting each to thread an owner through would have been 111
chances to forget one — where forgetting does not raise, it quietly serves somebody else's
resume.

So the *lookup* changed rather than its callers.

---

## What was built

### Identity — `backend/careeragent/auth/`

Four modules, all on the standard library. No new dependency was added for any of this.

**`passwords.py`** — `hashlib.scrypt` at OWASP's 2023 floor (n=2¹⁷, r=8, p=1). Parameters
travel *inside* the encoded string, so raising them later does not invalidate every existing
password: an old hash verifies against its own parameters and is re-hashed on the next
successful login — the one moment the plaintext exists to re-hash with. Verification goes
through `hmac.compare_digest`. A corrupt stored hash returns `False` rather than raising,
because a row nobody can authenticate against is the safe reading and an exception would
distinguish it from a wrong password.

Length is the only rule. Composition rules ("one capital, one symbol") push people towards
`Password1!` — measurably *less* entropy while feeling stricter, which is why NIST dropped
them.

**`tokens.py`** — HS256 JWTs in about sixty lines of `hmac`. A JWT rather than an opaque
random string for one reason: Supabase issues HS256 tokens signed with the project secret,
so one verifier accepts a Supabase session *and* one this system minted, with no second
notion of identity. Every documented footgun is closed deliberately, and each has a test:

| Closed | How |
|---|---|
| `alg: none` | the algorithm is read from our own constant, never from the token |
| tampered payload | signed bytes are compared as received, not re-serialised |
| forged signature | `hmac.compare_digest` |
| no expiry | rejected — "no `exp`" is not "never expires" |
| expired | 30s leeway for clock drift, no more |

**`service.py`** — register, authenticate, revoke. This is where the rules about what an
error may *say* live. Registration against a taken address and login with a wrong password
both fail generically, and an unknown email still pays the full scrypt cost against a decoy
hash — otherwise the response time answers what the message refuses to.

**`tenancy.py`** — a `ContextVar` holding the `Principal` this unit of work is for.
`contextvars` and not a thread-local, because this application is async and many requests
share a thread; a thread-local would hand one user's identity to another's coroutine.

### The line that changed

```python
principal = tenancy.current()
if principal is not None:
    return ...where(Candidate.user_id == principal.user_id)...

# Raises in multi-user mode; returns None in single-user mode.
tenancy.current_candidate_id()
return (await session.execute(select(Candidate).limit(1))).scalar_one_or_none()
```

**The fallback is a refusal, not a default.** With identity enforced and nobody bound, this
raises `NoPrincipal` — deliberately a 500 rather than a quiet read of the first row, because
the quiet read is a cross-tenant disclosure and the exception is a bug report.

Single-user mode keeps the old behaviour, and that is not a loophole: with one profile,
falling back to it is the correct answer, and demanding a login to read your own resume on
your own laptop is theatre with a real cost. One setting switches both halves — there is no
deployment where requests are authenticated but queries are unscoped.

### Schema

`app_user`, separate from `candidate` rather than merged into it. They answer different
questions and change at different rates: `candidate` is who you are *professionally* and is
rewritten every time a CV is parsed; `user` is who is holding the session. Merged, a password
hash would sit on the row the resume parser overwrites, and an account would be deletable by
re-uploading a file.

`suppression` gained a `candidate_id`. A shared do-not-contact list leaks in two directions
at once — one user's opt-out silences another's outreach to somebody who never spoke to them,
and the list itself records who you wrote to and who told you to stop. Rows with no owner
stay visible to everyone, which is the safe direction: it over-suppresses, and the cost of
over-suppressing is an unsent message.

### Endpoints and the login screen

```
POST /api/v1/auth/register · /login · /logout · /password
GET  /api/v1/auth/me
```

No response model in that file has a field for a password hash, which is a stronger
guarantee than remembering not to fill one in — and a test asserts it **on the wire**, so a
model that grows one fails immediately.

The dashboard gained a login gate: token in `localStorage` (not a cookie — a cookie is
attached automatically to requests this page did not make, which is a CSRF exposure a bearer
header does not have), an `api()` wrapper that attaches it, and a 401 handler that shows the
sign-in screen instead of letting twelve panels each render "failed to load".

### `careeragent account`

`list`, `create [--claim]`, `set-password`, `secret`. These have to exist off the network:
the migration claims the existing profile with an account that has **no password**, and in
multi-user mode every endpoint needs a token, and a token needs a login. Without a local
command that is a locked door with the key inside.

Passwords are prompted for, never taken as arguments — an argument is in the shell history,
the process list, and any terminal recording.

---

## Two bugs found, both by tests

**The dependency was attached to the wrong thing.** It was wired at `include_router`, which
covers the fifteen domain routers. But `api.py` declares its own endpoints with `@app.get` —
and `/api/v1/candidate`, *the parsed resume*, answered a caller with no token at all. The
isolation suite failed on it within a minute of first running.

The fix moved it to the application, where it covers every route that exists and every route
anybody adds later. That is the whole argument against opt-in auth in one example: the list
of protected endpoints is a list somebody forgets to extend, and forgetting does not raise.

**Revocation lost a race with the clock.** `tokens_valid_from` compared against the token's
`iat` — which is whole seconds — so a token issued in the *same second* as a logout compared
equal to the cutoff and survived it. Anyone logging out immediately after logging in kept
their session.

It passed when run alone and failed in the full suite, which is the tell: on a slower path
the two operations landed in different seconds. A timestamp was the wrong mechanism. Tokens
now carry a **serial**; revoking increments a column; a stale serial is refused. There is no
clock left in it to lose a race with. The timestamp stays for tokens minted elsewhere — a
Supabase session carries no serial of ours — and because "when were sessions last revoked" is
worth being able to answer.

---

## Verified

**On the live database.** Migrated after a snapshot; the corpus is intact — 1,748 jobs, 49
facts, 91 people — and the existing profile was claimed by an account created from the email
in the resume:

```
Email                            Name         Can log in    Profile
venkateshdhanabalan056@...       Venkatesh D  no password   yes
```

Single-user mode is unchanged: `/api/v1/candidate` still answers without a token, and
`/auth/me` reports `multi_user: false`.

**In a real browser**, against a throwaway two-account deployment: the sign-in screen
renders, a wrong password shows *"email or password is incorrect"* without wiping the form,
and a correct one loads the dashboard. Then, from the page's own console:

```json
{ "alice": { "name": "Alice Ng", "skills": ["haskell"] },
  "bob":   { "name": "Bob Rao",  "skills": ["fortran"] },
  "noToken": 401 }
```

Two accounts, one database, neither able to see the other.

---

## Tests added

30 new, in two files.

`tests/test_auth.py` — 15 on the primitives, every one a refusal: salts differ per hash,
an empty password is refused rather than hashed, a corrupt hash is `False` not an exception,
`alg: none` is refused, a tampered payload is refused, a token with no expiry is refused,
signing without a secret is refused.

`tests/test_tenant_isolation.py` — 15 through the **HTTP API**, not the repository. A unit
test that calls `get_or_create_candidate` with a principal bound proves the function respects
an argument; it proves nothing about the forty endpoints that never pass one, and the bug is
an endpoint that forgets. Every assertion is negative — "Bob sees his own data" is satisfied
by a system with no isolation at all:

* neither account sees the other's profile or Fact Bank
* the second account to register is **not** served the first one's profile — the exact
  failure `limit(1)` would have produced
* a do-not-contact list is not shared
* anonymous, forged, and deactivated-account tokens are all refused
* logging out refuses every token for that account and none for the other
* logging out within the same second still revokes *(the regression above)*
* registration does not reveal that an email is taken; login says the same thing for a wrong
  password and an unknown account
* no response anywhere contains a password hash
* unowned work raises instead of reading the first row — and single-user mode still falls
  back, which is the other half of the same decision

---

## What remains

**Per-person relevance is still global.** `person.relevance` is stored as "the last computed
value" — it was already documented as a wart, and multi-user turns it into a small leak:
user B sees a relevance computed for user A's career. It is a number, not a name, and the
people themselves are genuinely shared corpus. The fix is a `person_relevance(person_id,
candidate_id)` join table, and it belongs with the next piece of people work rather than
bolted on here.

**Supabase is wired but unexercised.** The verifier accepts a Supabase-signed token when
`CAREERAGENT_SUPABASE_JWT_SECRET` is set, and the account-provisioning path for a subject
this database has not seen is deliberately *not* implemented — trusting an unknown id from
an external token is exactly the thing not to do quietly. It needs a real project to test
against. See [required_api_keys.md](../../required_api_keys.md).

**No password reset by email.** Deliberately: it needs a send path that is not the outreach
Gmail transport, and an account-recovery flow built carelessly is a better way in than the
password.
