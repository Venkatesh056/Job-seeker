# Phase 2 — Fixing the discovery yield

**Status: complete.** Tests at end of phase: 1,300 passing.

The plan was Apify, for JavaScript-rendered team pages. Apify turned out not to be the
answer — but measuring it is what found the real bug.

**Result: 2 people across 8 companies → 91 people across 8 companies, 29 worth contacting.**

---

## What Apify actually told me

A rendered crawl of `quantiphi.com/leadership`:

```
finished SUCCEEDED in 33s | cost $0.0042
```

Budget was never the problem — $5 buys about 1,180 pages. But the result came back with:

```json
{"crawl": {"loadedUrl": "https://quantiphi.com/"}}
```

**The page does not exist.** `/leadership` 302s to the homepage, which answers 200 with
`text/html`. So the crawler had been parsing the *homepage* and reporting its marketing copy
as staff. Every "person" it found there was a section heading or a customer testimonial —
"Industry Leadership", "Phi Labs", and a quote from the President of the Employers' Forum of
Indiana.

The earlier diagnosis, "these pages are JavaScript-rendered", was half right and pointed at
the wrong fix.

---

## Three attempts, in the order they were found

### 1. Reject a redirected page

A guessed path that lands somewhere else is not the page we asked for.

```python
def _same_page(landed: str, asked: str) -> bool:
    # http → https and a trailing slash are the same page. A redirect to "/" is not.
```

This took the yield to **zero** — honestly zero, where it had been two people and several
fictions.

### 2. Read the site's own links

Guessing paths is the wrong shape of solution. A company that has a leadership page links
to it and says so in the anchor text.

Measured: **sigmoid.com has 196 anchors and its only "About" link is `href="#"`**, because
modern navigation is a JavaScript dropdown. Also zero — but correct, and it works on sites
that publish their navigation.

### 3. Ask a search provider where the page actually is

This is the one that works. Search engines have already rendered these sites, so they know
the real URL:

```
https://quantiphi.com/company/executive-team
https://razorpay.com/newsroom/leadership-team
https://www.sigmoid.com/about-sigmoid/
```

None of which any path list would produce. One Tavily credit per company, against a
1,000-credit monthly budget.

Restricted to the company's own domain: `theorg.com/org/razorpay/teams/leadership-team` is a
third-party directory, and this reads what a company publishes about itself.

---

## Five bugs the real data caught

Two are the same class, and half of it had been fixed before.

**`"ai"` matched the start of `"Aiven"`.** So `function_of("Chief Financial Officer, Aiven")`
returned `ai_ml`, and Aiven's CFO scored **85/100** for a machine-learning candidate — as did
its VP of People and its Chief Revenue Officer. Exactly the shape of `cto` matching inside
`dire`**cto**`r`: a leading word boundary was added for that, and this is the trailing half.
Short markers now need a boundary at both ends; longer ones stay open because several are
deliberately prefixes (`recruit` → recruiter, `financ` → financial).

**An anchor regex contained `\b` in a non-raw string**, making it a literal backspace
character. `<a\x08` can never match, so link discovery was silently dead — and the people
found before this was noticed came from search alone.

**`"Chief Strategy Officer"` was stored as a person.** Its subject matter is in no function
vocabulary, so the existing "a title is not a name" test missed it. A phrase containing a
role word is now never a name.

**`"Our Leaders"` and `"Additional Information"`** were stored as employees of Kredx and
Druva. Two capitalised words each; both pass every structural test a name passes. The
opening word gives them away.

**Mayur Rustagi appeared twice** — "CTO and Co-Founder" and "Chief Technology Officer and
Co-Founder" do not contain one another. Titles now compare by function and level, so one man
is one record.

---

## Measured outcome

```
91 people across 8 companies
29 worth contacting (relevance ≥ 45)
47 with a LinkedIn profile
 0 with a published email — and none guessed
```

Highest-ranked contact: **Mirza Aasim Baig, Lead AI Ops Engineer at Karya — 84/100**,
"works directly in ai/ml, your target area". Real executives found include Harshil Mathur
(Razorpay CEO & Co-founder), Mayur Rustagi (Sigmoid CTO), Jim Reesing (Quantiphi CEO).

Zero published emails is an honest result: none of these companies publishes personal
addresses, and nothing invented one.

---

## Why Apify is not wired in

It works, and at $0.0042 a page it is affordable. But search solved the problem for a tenth
of the effort and none of the credit. The $5 stays unspent until something genuinely needs
rendering — a site search cannot reach, most likely.

The decision is recorded rather than the code, so it can be revisited cheaply.
