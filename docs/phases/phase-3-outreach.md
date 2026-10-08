# Phase 3 — Outreach that closes the loop

**Status: core complete.** Tests at end of phase: 1,383 passing (up from 1,300).

Sending became real in an earlier phase, when the Gmail transport landed. This phase builds
what has to exist *around* a real send: the list of people who must not be written to, the
rule that a message is a message whichever channel it arrives on, and messages that could
not have been sent to anybody else.

---

## What was built

### Suppression — the list that has to be right

`backend/careeragent/outreach/suppression.py`, table `suppression`, migration `11a712a278b6`

Every other outreach failure can be walked back. A badly personalised email is
embarrassing; a duplicate is annoying; writing to somebody who asked you to stop cannot be
undone. So these rules are absolute rather than weighted, and **checked at the transport
boundary** rather than trusted to callers.

Four reasons, and they mean different things:

| Reason | What it is about | Who can lift it |
|---|---|---|
| `OPTED_OUT` | the person | user, with an explicit `force` |
| `BOUNCED` | the address | user |
| `DO_NOT_CONTACT` | the user's decision | user |
| `WRONG_PERSON` | the record | user |

The distinction matters when something changes. A bounced address can be superseded by a
better one; an opt-out cannot be superseded by anything. Collapsing them into one
`suppressed` boolean would lose that, and the first time a new address was found for an
opted-out contact the system would happily write to them again.

Checked in **three** places, because there are three ways to reach a person:
`deliver()` before sending, `send()` before marking something sent by hand, and `/compose`
before a draft is even written — telling the user after they have read a draft wastes their
attention on a decision already made.

A reply that asks to stop suppresses automatically. The matching is deliberately narrow:

```python
# "not interested in receiving" was here and is deliberately gone: it matched
# "not interested in receiving a discount, but tell me about the role", which is
# somebody continuing the conversation, not ending it.
```

A false positive here silently ends a real conversation, which is worse than missing one
and leaving it to the user.

### Cross-channel duplicate protection

`ContactHistory` in `backend/careeragent/applications/outreach.py`

A candidate who emailed somebody yesterday and connects on LinkedIn today has messaged them
twice, and **the recipient experiences it as twice however the sender files it**. Channels
are an implementation detail of the sender's tooling, not of the person reading — so this
is one question across all of them, not one per channel.

```
you contacted them today on email. A second message inside 7 days reads as
pressure, whichever channel it arrives on.
```

A reply outranks the window entirely: somebody who answered is in a conversation, and
starting a fresh thread at them is worse than waiting however long ago it was.

### Messages written to a person, not to a company

`backend/careeragent/outreach/compose.py`, prompt `person_message@1.0`

The existing prompt writes a good speculative note to a *company*. This writes to a *person*
about a *specific opening*, and three things change it.

**Who is reading.** Four audiences, each asking for something different:

| Audience | Leads with |
|---|---|
| Recruiter | the role, the skills it names, availability, the attached resume |
| Engineering leader | one concrete project, overlapping technology, why this role |
| Executive | the strongest result, its domain, why this product |
| Peer | shared technical ground and a specific question |

A founder at a twelve-person company routes to **engineering leader**, not executive —
that is the hat they will be wearing when they read it.

**Which channel.** Email carries an attachment and can afford 150 words. LinkedIn has no
attachment, is read on a phone, and dies past 80. A LinkedIn message also has **no subject
line**; offering one invites the user to write something that is silently discarded.

**What is true.** Company detail comes from the researched profile and nothing else, which
is what stops a message praising a product the company never shipped. Every claim cites a
Fact Bank id.

Two checks that run on drafts and on hand-edited messages alike, because a user's edit is
not more trustworthy than a model's draft simply because a human made it:

* **Template openings** — "I hope this finds you well", "I came across your company",
  "My name is", "To whom it may concern"
* **Fabricated rapport** — "as we discussed", "our mutual connection", "I was referred to",
  "I have been following your work", "your recent post". Each is a lie, and each is what a
  model reaches for when it has nothing specific to say. The warning says *removed, not
  softened*.

Subject lines are offered several at a time. A subject is read by everyone who receives the
message and rewritten by nobody, so offering one is offering too few.

### Endpoints

`POST /outreach/compose` · `POST /outreach/compose/check` ·
`GET|POST|DELETE /outreach/suppressed`

---

## Verified with real data

Drafted to **Mirza Aasim Baig, Lead AI Ops Engineer at Karya (84/100)** through the API,
with the live model:

**Email** — 84 words, audience `engineering_leader`, attachment on, subject
"Machine Learning Engineer at Karya":

> …I implemented a 3-factor authentication system for TRI-BIOMETRIX using Python,
> TensorFlow, OpenCV and Flask… My tailored resume is attached for your review. Are you
> available to discuss how these technical skills align with the current priorities of your
> team?

**LinkedIn** — 57 words, no subject, no attachment, a different angle:

> …I also engineered a graph-based fraud detection system with PyTorch and Neo4j… How could
> my experience with multimodal authentication and graph analytics support Karya's AI Ops
> initiatives?

Both cite real Fact Bank ids. Zero warnings. Genuinely different messages, not one text
with the salutation swapped.

---

## Bugs found while building

**A message counted itself as prior contact.** An approved-but-unsent record appeared in
its own history and blocked its own delivery. Two fixes: `approved` joined the statuses the
recipient has experienced nothing of — approval is a decision by the *sender* — and the
message being delivered is excluded from its own lookup.

**Suppression was not idempotent.** The de-duplication read an id out of a note that never
contained one, so every call inserted another row.

**A founder at a tiny company was pitched as a distant executive**, contradicting the
module's own docstring.

**A LinkedIn draft carried a subject line**, which LinkedIn has nowhere to put.

---

## What remains in this phase

* **Campaigns** — a campaign object with per-campaign limits, target sets, approval mode
* **Reply detection** — needs the `gmail.readonly` scope, deliberately not requested yet;
  it is a much broader ask than `gmail.send` and should be a separate opt-in
* **Bounce detection** — same scope problem; the suppression reason exists and can be set
  by hand today
* **The full status machine** — `Delivered`, `Follow-up Due`, `Interested`, `Not Interested`
  as distinct states rather than the current `sent / replied / declined / bounced`
* **An approval screen** showing recipient, source, relevance, message, resume and prior
  contact in one place. The data is all there; the screen is not.
