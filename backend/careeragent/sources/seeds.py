"""Curated company target lists.

Discovery is only as good as the companies you point it at, and the default
examples in most job tools are US megacaps — exactly wrong for someone looking
for an early-career role in India. A list of Stripe, Ramp and Palantir produces
senior US roles and nothing else.

Every entry below was **verified live on 2026-09-12**: 254 candidate domains
were probed, the ATS resolved, and the board returned real openings. 59
companies had a readable board — a 23% hit rate — together exposing about 1,550
jobs. Domains that resolved to nothing are kept in `NO_PUBLIC_BOARD` rather than
quietly dropped, because "we cannot see this company" is useful information.

Job counts are from that run and will drift; they indicate board size, not a
promise. Slugs are deliberately NOT stored: the `ats_board` registry learns them
at run time, and the careers-page fallback resolves the awkward ones — Razorpay's
board is `razorpaysoftwareprivatelimited`, which no guess would produce.
"""

from __future__ import annotations

# ─── Verified boards, largest first ─────────────────────────────────────────
# Mixed by design: consumer tech, fintech, SaaS, AI/data services and edtech,
# weighted toward companies that hire in India rather than toward brand size.
INDIA_VERIFIED: tuple[str, ...] = (
    "paytm.com",  # lever, ~211 jobs
    "stage.in",  # greenhouse, ~141 jobs
    "freshworks.com",  # smartrecruiters, ~139 jobs
    "apna.co",  # workable, ~129 jobs
    "swiggy.com",  # smartrecruiters, ~75 jobs
    "postman.com",  # greenhouse, ~64 jobs
    "sarvam.ai",  # ashby, ~62 jobs
    "meesho.com",  # lever, ~49 jobs
    "glance.com",  # greenhouse, ~45 jobs
    "druva.com",  # greenhouse, ~41 jobs
    "quantiphi.com",  # workday, ~40 jobs
    "sprinklr.com",  # workday, ~40 jobs
    "sigmoid.com",  # greenhouse, ~35 jobs
    "browserstack.com",  # workday, ~33 jobs
    "signzy.com",  # keka, ~31 jobs
    "hackerrank.com",  # greenhouse, ~29 jobs
    "porter.in",  # lever, ~26 jobs
    "razorpay.com",  # greenhouse, ~22 jobs
    "turing.com",  # greenhouse, ~21 jobs
    "fi.money",  # lever, ~19 jobs
    "mindtickle.com",  # lever, ~19 jobs
    "zeta.tech",  # lever, ~18 jobs
    "clickpost.ai",  # keka, ~16 jobs
    "karbon.business",  # greenhouse, ~15 jobs
    "karya.in",  # greenhouse, ~15 jobs
    "cloudsek.com",  # greenhouse, ~14 jobs
    "cred.club",  # lever, ~11 jobs
    "epifi.com",  # lever, ~11 jobs
    "lead.school",  # teamtailor, ~10 jobs
    "springboard.com",  # greenhouse, ~10 jobs
    "crio.do",  # rippling, ~9 jobs
    "atlan.com",  # ashby, ~8 jobs
    "gramophone.in",  # smartrecruiters, ~8 jobs
    "kredx.com",  # smartrecruiters, ~8 jobs
    "minimalist.co",  # recruitee, ~8 jobs
    "fractal.ai",  # teamtailor, ~7 jobs
    "levelaccess.com",  # greenhouse, ~7 jobs
    "open.money",  # personio, ~7 jobs
    "zluri.com",  # keka, ~6 jobs
    "pocketfm.com",  # lever, ~5 jobs
    "shipsy.io",  # smartrecruiters, ~5 jobs
    "zepto.co.in",  # bamboohr, ~5 jobs
    "wint.com",  # teamtailor, ~4 jobs
    "e2e.co.in",  # breezy, ~3 jobs
    "instahyre.com",  # smartrecruiters, ~3 jobs
    "josh.in",  # personio, ~3 jobs
    "unacademy.com",  # smartrecruiters, ~3 jobs
)

# ─── Verified but low confidence ────────────────────────────────────────────
# These returned only one or two jobs. Several providers answer 200 for a slug
# that belongs to a different company entirely, so a near-empty board may be a
# name collision rather than a quiet employer. Kept, but separated: worth
# re-checking before trusting a "no openings" result from any of them.
INDIA_LOW_CONFIDENCE: tuple[str, ...] = (
    "absolutegroup.in",  # bamboohr, ~2 jobs
    "axio.in",  # rippling, ~2 jobs
    "cropin.com",  # keka, ~2 jobs
    "interviewbit.com",  # smartrecruiters, ~1 jobs
    "lendingkart.com",  # smartrecruiters, ~1 jobs
    "newtonschool.co",  # smartrecruiters, ~1 jobs
    "ramco.com",  # recruitee, ~2 jobs
    "rebelfoods.com",  # smartrecruiters, ~1 jobs
    "turtlemint.com",  # smartrecruiters, ~1 jobs
    "upstox.com",  # smartrecruiters, ~1 jobs
    "weekday.works",  # recruitee, ~1 jobs
    "whatfix.com",  # smartrecruiters, ~1 jobs
)

# ─── Global, remote-friendly, open to junior applicants ─────────────────────
# A supplement to the India lists, not the core. Deliberately not megacaps.
GLOBAL_REMOTE_FRIENDLY: tuple[str, ...] = (
    "gitlab.com",
    "hotjar.com",
    "doist.com",
    "aiven.io",
    "grafana.com",
    "supabase.com",
    "posthog.com",
)

SEGMENTS: dict[str, tuple[str, ...]] = {
    "india": INDIA_VERIFIED,
    "india-all": INDIA_VERIFIED + INDIA_LOW_CONFIDENCE,
    "remote": GLOBAL_REMOTE_FRIENDLY,
    "all": INDIA_VERIFIED + INDIA_LOW_CONFIDENCE + GLOBAL_REMOTE_FRIENDLY,
}

# ─── The honest gap ─────────────────────────────────────────────────────────
# 195 of 254 probed domains exposed no readable board. Adding Keka and Freshteam
# recovered several; the rest mostly run Darwinbox, Zoho Recruit, PeopleStrong,
# SuccessFactors or a bespoke portal, none of which publish an unauthenticated
# feed.
#
#   Darwinbox    an Angular app whose job-list call was not reachable from the
#                public careers page; needs more investigation.
#   Zoho Recruit server-rendered with no JSON feed and no JSON-LD.
#
# Until those land, the honest answer for these companies is that we cannot see
# their openings — which beats reporting "no jobs found".
NO_PUBLIC_BOARD: tuple[str, ...] = (
    "1mg.com",
    "absolutdata.com",
    "acko.com",
    "affine.ai",
    "agrostar.in",
    "almabetter.com",
    "amagi.com",
    "angelone.in",
    "apnamart.in",
    "appsecco.com",
    "beardo.in",
    "bigbasket.com",
    "bijak.in",
    "billdesk.com",
    "birlasoft.com",
    "blinkit.com",
    "blusmart.com",
    "boat-lifestyle.com",
    "bombayshavingcompany.com",
    "bounceshare.com",
    "brillio.com",
    "byjus.com",
    "capillarytech.com",
    "capitalfloat.com",
    "cashfree.com",
    "chaayos.com",
    "chaloapp.com",
    "chargebee.com",
    "chingari.io",
    "classplusapp.com",
    "cleartax.in",
    "countrydelight.in",
    "course5i.com",
    "crayondata.com",
    "credgenics.com",
    "cuemath.com",
    "cure.fit",
    "cutshort.io",
    "cyient.com",
    "darwinbox.com",
    "decentro.tech",
    "dehaat.com",
    "delhivery.com",
    "dhan.co",
    "digitalocean.com",
    "digitinsurance.com",
    "dreamsports.group",
    "dunzo.com",
    "e2enetworks.com",
    "easebuzz.in",
    "emeritus.org",
    "eruditus.com",
    "even.in",
    "exotel.com",
    "fareye.com",
    "firstcry.com",
    "fisdom.com",
    "flipkart.com",
    "freshtohome.com",
    "gamesk.in",
    "gofynd.com",
    "gonoise.com",
    "gramener.com",
    "greatlearning.in",
    "gupshup.io",
    "hackerearth.com",
    "happiestminds.com",
    "haptik.ai",
    "hasura.io",
    "healthifyme.com",
    "hirect.in",
    "icertis.com",
    "incedoinc.com",
    "indmoney.com",
    "infra-market.com",
    "innovaccer.com",
    "instamojo.com",
    "insurancedekho.com",
    "intellectdesign.com",
    "jai-kisan.com",
    "jumbotail.com",
    "juspay.in",
    "kaleyra.com",
    "keka.com",
    "khatabook.com",
    "kissflow.com",
    "kissht.com",
    "knowlarity.com",
    "krutrim.com",
    "kuku.fm",
    "lambdatest.com",
    "latentview.com",
    "leadsquared.com",
    "lenskart.com",
    "licious.com",
    "locus.sh",
    "lucideus.com",
    "m2pfintech.com",
    "mad-street-den.com",
    "mamaearth.in",
    "manthan.com",
    "marg.in",
    "mcaffeine.com",
    "medibuddy.in",
    "mfine.co",
    "mindtree.com",
    "mobikwik.com",
    "moglix.com",
    "moneyview.in",
    "mpl.live",
    "mu-sigma.com",
    "myntra.com",
    "nazara.com",
    "netcore.co.in",
    "newgensoft.com",
    "neysa.ai",
    "ninjacart.in",
    "niramai.com",
    "nucleussoftware.com",
    "nykaa.com",
    "observe.ai",
    "ofbusiness.com",
    "okcredit.in",
    "ola.ai",
    "onsurity.com",
    "paysense.in",
    "payu.in",
    "peoplestrong.com",
    "perfios.com",
    "persistent.com",
    "phable.io",
    "pharmeasy.in",
    "phonepe.com",
    "plivo.com",
    "plumgoodness.com",
    "plumhq.com",
    "policybazaar.com",
    "practo.com",
    "predible.health",
    "purplle.com",
    "pw.live",
    "qaprosoft.com",
    "qure.ai",
    "rapido.bike",
    "recur.club",
    "safehats.com",
    "scaler.com",
    "scripbox.com",
    "senseforth.ai",
    "seqrite.com",
    "setu.co",
    "sharechat.com",
    "shiprocket.in",
    "sigtuple.com",
    "simplilearn.com",
    "sirionlabs.com",
    "skit.ai",
    "smallcase.com",
    "sonata-software.com",
    "springworks.in",
    "stellapps.com",
    "stockgro.com",
    "subconscious.ai",
    "sugarcosmetics.com",
    "tally.solutions",
    "teachmint.com",
    "testsigma.com",
    "thesleepcompany.in",
    "thirdwavecoffee.in",
    "tigeranalytics.com",
    "traceable.ai",
    "tredence.com",
    "tricog.com",
    "u-gro.com",
    "udaan.com",
    "unicommerce.com",
    "uniphore.com",
    "upgrad.com",
    "vedantu.com",
    "verloop.io",
    "vinculumgroup.com",
    "vymo.com",
    "wadhwaniai.org",
    "wakefit.co",
    "waycool.in",
    "wellfound.com",
    "whitehatjr.com",
    "wingify.com",
    "yellow.ai",
    "yulu.bike",
    "zensar.com",
    "zetwerk.com",
    "zoho.com",
    "zolve.com",
    "zomato.com",
)


def segment(name: str) -> tuple[str, ...]:
    """Targets for a named segment. Raises with the valid names on a typo."""
    try:
        return SEGMENTS[name]
    except KeyError:
        raise ValueError(
            f"Unknown segment '{name}'. Choose from: {', '.join(sorted(SEGMENTS))}"
        ) from None
