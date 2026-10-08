"""Playwright application runner.

Execution rules, from docs/01-architecture.md §7. Each exists because of a
specific way these systems fail quietly:

  1. Verify every write. A combobox will accept keystrokes and commit something
     unrelated; unverified, that lands wrong data on a real application.
  2. Never fill sensitive fields. Demographics, sponsorship declarations and
     access codes go to the human at every autonomy level.
  3. Evidence or it did not happen. APPLIED requires a post-submit signal;
     anything weaker is SUBMISSION_UNVERIFIED.
  4. Screenshot every step — the evidence the user reviews and the only way to
     debug an adapter that broke on a form we cannot see.
  5. Stop for CAPTCHA, MFA, or login. There is no setting that bypasses this.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import get_settings
from ..logging import get_logger
from ..models.candidate import Candidate
from .adapters import EXTRACT_JS, FormSpec, detect_ats, may_auto_submit
from .catalog import never_fill
from .resolver import Resolution, resolve_all
from .verdict import Failure, presubmit_verdict, value_matches

if TYPE_CHECKING:
    from playwright.async_api import Page

log = get_logger("browser.runner")

# Signals that a human must take over. Never bypassed, at any autonomy level.
BLOCKERS = (
    ("captcha", r"captcha|recaptcha|hcaptcha|turnstile|cf-challenge"),
    ("mfa", r"two-?factor|2fa|verification code|one-?time (code|password)|authenticator"),
    ("login", r"sign in to (apply|continue)|log ?in to (apply|continue)|create an account"),
    ("identity", r"upload.*(passport|government|id document)|identity verification"),
)

# Read back the option a <select> actually landed on. Selecting by label can
# silently pick a different option when labels are near-duplicates, so the
# commit is always verified rather than assumed.
SELECTED_LABEL_JS = """
    el => {
        const option = el.options[el.selectedIndex];
        return option ? option.text : '';
    }
"""

# Where combobox options live across the ATS platforms we support.
OPTION_SELECTORS = (
    "[role=option]",
    "[class*='option']:not(option)",
    "li[id*='option']",
    ".select__option",
)

# Post-submit confirmation. Nothing weaker is accepted as proof.
CONFIRMATION = re.compile(
    r"thank you for (applying|your application)"
    r"|application (was )?(received|submitted|complete)"
    r"|we('| ha)ve received your application"
    r"|your application has been",
    re.IGNORECASE,
)


# Per-field write timeout. Playwright's 30s default means one rotted selector costs
# half a minute; ten of them cost five minutes. A field that is not ready in this
# long is not going to be.
FILL_TIMEOUT_MS = 6_000


def option_labels(options: list[dict[str, Any]] | None) -> list[str]:
    for key in ("label", "text", "value"):
        labels = [str(o.get(key) or "").strip() for o in (options or []) if isinstance(o, dict)]
        if any(labels):
            return [label for label in labels if label]
    return []


def matching_option(value: str, options: list[dict[str, Any]] | None) -> str | None:
    """The option this value should select, or None if none of them fit.

    Pure, so the "can this dropdown even take this value?" question is answerable
    without a browser — which is what lets the runner skip a doomed write instead of
    waiting out its timeout.
    """
    labels = option_labels(options)
    if not labels:
        return None
    for label in labels:
        if value_matches(value, label):
            return label
    return None


class Outcome(enum.StrEnum):
    READY = "ready"
    HELD = "held"  # presubmit verdict blocked it
    NEEDS_USER = "needs_user"  # unanswerable fields
    HUMAN_REQUIRED = "human_required"  # captcha / MFA / login
    SUBMITTED = "submitted"  # confirmed
    UNVERIFIED = "unverified"  # clicked, no confirmation seen
    FAILED = "failed"


@dataclass
class StepRecord:
    canonical_field: str
    label: str
    action: str
    ok: bool
    detail: str = ""
    # What was actually written. Needed by the verification agent, which compares the
    # submitted form against the resume being attached to it — a check that silently
    # passes everything if it cannot see the values.
    value: str = ""


@dataclass
class RunResult:
    outcome: Outcome
    url: str
    ats: str = "generic"
    confidence: float = 0.0
    spec: FormSpec | None = None
    filled: list[StepRecord] = field(default_factory=list)
    needs_user: list[str] = field(default_factory=list)
    never_filled: list[str] = field(default_factory=list)
    holds: list[Failure] = field(default_factory=list)
    blocker: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    screenshots: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def submitted(self) -> bool:
        return self.outcome is Outcome.SUBMITTED


class BrowserRunner:
    """Drives one application. One runner per application, not per session."""

    def __init__(self, headless: bool = True, artifacts_dir: Path | None = None) -> None:
        settings = get_settings()
        self.headless = headless
        self.artifacts = artifacts_dir or Path(settings.storage_dir) / "applications"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        # Optional until `__aenter__` starts them. Every method that uses them runs
        # inside the context manager, so the narrowing below is a formality — but an
        # unannotated None makes mypy treat every later use as an error.
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._held_page: Page | None = None

    async def __aenter__(self) -> BrowserRunner:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=False,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                await closer.close()
        if self._playwright is not None:
            await self._playwright.stop()

    # ─── Reading ────────────────────────────────────────────────────────────

    async def read_form(self, page: Page, url: str) -> tuple[FormSpec, float]:
        html = await page.content()
        adapter, confidence = detect_ats(url, html)
        payload = await page.evaluate(EXTRACT_JS)
        spec = adapter.parse(payload, url)
        log.info(
            "form_read",
            ats=adapter.name,
            confidence=confidence,
            fields=len(spec.fields),
            required=len(spec.required_fields),
        )
        return spec, confidence

    async def detect_blocker(self, page: Page) -> str:
        """Anything that must hand control to a human."""
        try:
            html = (await page.content()).lower()
        except Exception:
            return ""
        for name, pattern in BLOCKERS:
            if re.search(pattern, html):
                return name
        # An iframe from a challenge provider is conclusive even when the page
        # text says nothing.
        for frame in page.frames:
            if re.search(r"recaptcha|hcaptcha|turnstile", frame.url or "", re.IGNORECASE):
                return "captcha"
        return ""

    # ─── Filling ────────────────────────────────────────────────────────────

    async def fill_field(
        self, page: Page, selector: str, kind: str, value: str
    ) -> tuple[bool, str]:
        """Write one value and verify it landed.

        Returns (ok, detail). A combobox whose readback is unrelated to what we
        typed is a failure, not a warning: continuing would submit wrong data.
        """
        try:
            locator = page.locator(selector).first
            if kind == "file":
                await locator.set_input_files(value, timeout=FILL_TIMEOUT_MS)
                count = await locator.evaluate("el => el.files ? el.files.length : 0")
                return (count > 0, f"{count} file(s) attached")

            if kind == "select":
                await locator.select_option(label=value, timeout=FILL_TIMEOUT_MS)
                actual = await locator.evaluate(SELECTED_LABEL_JS)
                return (value_matches(value, actual), f"committed {actual!r}")

            if kind in ("checkbox", "radio"):
                await locator.check(timeout=FILL_TIMEOUT_MS)
                checked = await locator.is_checked()
                return (checked, "checked" if checked else "did not check")

            await locator.fill("", timeout=FILL_TIMEOUT_MS)
            await locator.type(value, delay=15, timeout=FILL_TIMEOUT_MS)
            if kind == "combobox":
                await self._commit_combobox(page, locator, value)
            actual = await locator.input_value()
            ok = value_matches(value, actual)
            return (ok, f"committed {actual!r}" if ok else f"wanted {value!r}, got {actual!r}")

        except Exception as exc:
            return (False, f"{type(exc).__name__}: {exc}"[:200])

    async def _commit_combobox(self, page: Page, locator, value: str) -> None:
        """Commit a combobox by clicking its listbox option.

        NOT by pressing Enter. Enter inside a <form> submits the form — which is
        how an unattended run can fire off a half-filled application. The
        fixture tripwire caught exactly that, and there is no safe way to press
        Enter here, so we never do.
        """
        await asyncio.sleep(0.35)  # let the widget's listbox render
        for selector in OPTION_SELECTORS:
            options = page.locator(selector)
            try:
                count = await options.count()
            except Exception:
                continue
            for index in range(min(count, 12)):
                option = options.nth(index)
                try:
                    if not await option.is_visible():
                        continue
                    text = (await option.inner_text()).strip()
                except Exception:
                    continue
                if text and value_matches(value, text):
                    with contextlib.suppress(Exception):
                        await option.click()
                        await asyncio.sleep(0.2)
                        return
        # No listbox appeared. The typed value stands, and the read-back check
        # in the caller decides whether that is acceptable.

    async def fill_form(
        self,
        page: Page,
        spec: FormSpec,
        candidate: Candidate,
        resume_path: str | None = None,
        *,
        overrides: dict[str, str] | None = None,
        cover_letter: str | None = None,
        cover_letter_path: str | None = None,
    ) -> RunResult:
        """Fill what can be evidenced, plus anything supplied explicitly.

        `overrides` is how generated answers and user-approved corrections reach a
        form. It is checked BEFORE the resolver, so an approved value always wins —
        but it can never reach a NEVER_FILL field: that check runs first and no
        argument to this function can move it. A demographic question stays the
        human's to answer even when something upstream supplied a value for it.
        """
        result = RunResult(outcome=Outcome.READY, url=spec.url, ats=spec.ats)
        overrides = overrides or {}

        field_ids = [f.canonical_field for f in spec.fields]
        resolved = resolve_all(field_ids, candidate, resume_path=resume_path)

        for form_field in spec.fields:
            canonical = form_field.canonical_field
            decision = resolved[canonical]

            # Ordered first and deliberately unreachable by `overrides`.
            if never_fill(canonical) or decision.resolution is Resolution.NEVER_FILL:
                result.never_filled.append(form_field.label)
                continue

            value = ""
            if canonical in overrides:
                value = overrides[canonical]
            elif form_field.label in overrides:
                value = overrides[form_field.label]
            elif canonical == "cover_letter.text" and cover_letter:
                value = cover_letter
            elif canonical == "cover_letter.file" and cover_letter_path:
                value = cover_letter_path
            elif decision.resolution is Resolution.FILL:
                value = decision.value

            if not value:
                if form_field.required:
                    reason = decision.reason or "no value available"
                    result.needs_user.append(f"{form_field.label} — {reason}")
                continue

            # A dropdown whose options cannot express this value is not ours to
            # answer. "0.2 years of experience" has to become "0-1 years" or
            # "1-2 years", and which one is a claim about the candidate — so it goes
            # to them rather than being rounded by the agent.
            if form_field.kind in {"select", "radio", "checkbox"} and form_field.options:
                chosen = matching_option(value, form_field.options)
                if chosen is None:
                    choices = ", ".join(option_labels(form_field.options)[:6])
                    result.needs_user.append(
                        f"{form_field.label} — {value!r} is not one of the options "
                        f"({choices}); pick the right one"
                    )
                    continue
                value = chosen

            ok, detail = await self.fill_field(page, form_field.selector, form_field.kind, value)
            result.filled.append(
                StepRecord(
                    canonical_field=canonical,
                    label=form_field.label,
                    action="fill",
                    ok=ok,
                    detail=detail,
                    value=value,
                )
            )
            if not ok:
                log.warning(
                    "fill_unverified",
                    field=canonical,
                    label=form_field.label,
                    detail=detail,
                )

        return result

    # ─── Verifying and submitting ───────────────────────────────────────────

    async def verify(self, page: Page, spec: FormSpec) -> list[Failure]:
        """Re-read the page and decide. The JS gathers; Python judges."""
        payload = await page.evaluate(EXTRACT_JS)
        from .adapters import ADAPTERS_BY_NAME

        adapter = ADAPTERS_BY_NAME.get(spec.ats) or ADAPTERS_BY_NAME["generic"]
        fresh = adapter.parse(payload, spec.url)
        return presubmit_verdict([f.to_state() for f in fresh.fields])

    async def submit(self, page: Page, spec: FormSpec) -> tuple[Outcome, dict[str, Any]]:
        """Activate submit, then look for proof. No proof means UNVERIFIED.

        Reporting an unverified click as APPLIED would corrupt every downstream
        metric and, worse, suppress a retry the candidate needed.
        """
        if not spec.submits:
            return Outcome.FAILED, {"reason": "no submit control found"}

        target = next((s for s in spec.submits if not s.get("disabled")), None)
        if target is None:
            return Outcome.HELD, {"reason": "submit control is disabled"}

        before = page.url
        try:
            await page.locator(target["selector"]).first.click()
        except Exception as exc:
            return Outcome.FAILED, {"reason": f"click failed: {exc}"[:200]}

        # A single-page form may never go idle; the content check below decides.
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=15_000)

        content = await page.content()
        evidence: dict[str, Any] = {
            "url_before": before,
            "url_after": page.url,
            "at": datetime.now(UTC).isoformat(),
        }

        if match := CONFIRMATION.search(content):
            evidence["confirmation_text"] = match.group(0)
            return Outcome.SUBMITTED, evidence
        if page.url != before and re.search(r"thank|confirm|success|submitted", page.url, re.I):
            evidence["confirmation_url"] = page.url
            return Outcome.SUBMITTED, evidence

        evidence["reason"] = "no confirmation text or URL change detected"
        return Outcome.UNVERIFIED, evidence

    @property
    def held_page(self) -> Page | None:
        """The page from a `keep_open=True` run, if it is still alive."""
        page = self._held_page
        if page is None or page.is_closed():
            return None
        return page

    async def submit_held(
        self, spec: FormSpec, *, overrides: dict[str, str] | None = None
    ) -> tuple[Outcome, dict[str, Any], list[Failure]]:
        """Submit the held page after re-verifying it. Used by the approval path.

        Re-reads and re-judges before clicking, because time passed and a human may
        have touched the form. An approval authorises submitting a form that passes
        the verdict — not submitting regardless of it.
        """
        page = self.held_page
        if page is None:
            return (Outcome.FAILED, {"reason": "the reviewed browser session has closed"}, [])

        if overrides:
            from .adapters import ADAPTERS_BY_NAME

            adapter = ADAPTERS_BY_NAME.get(spec.ats) or ADAPTERS_BY_NAME["generic"]
            fresh = adapter.parse(await page.evaluate(EXTRACT_JS), spec.url)
            for form_field in fresh.fields:
                if never_fill(form_field.canonical_field):
                    continue
                value = overrides.get(form_field.canonical_field) or overrides.get(
                    form_field.label, ""
                )
                if value:
                    await self.fill_field(page, form_field.selector, form_field.kind, value)

        if blocker := await self.detect_blocker(page):
            return (Outcome.HUMAN_REQUIRED, {"blocker": blocker}, [])

        holds = await self.verify(page, spec)
        if holds:
            return (Outcome.HELD, {"reason": "pre-submit verdict blocked submission"}, holds)

        outcome, evidence = await self.submit(page, spec)
        return (outcome, evidence, [])

    async def screenshot(self, page: Page, name: str) -> str:
        path = self.artifacts / f"{name}-{datetime.now(UTC):%Y%m%d-%H%M%S}.png"
        try:
            await page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception as exc:
            log.warning("screenshot_failed", error=str(exc)[:120])
            return ""

    # ─── Orchestration ──────────────────────────────────────────────────────

    async def apply(
        self,
        url: str,
        candidate: Candidate,
        *,
        resume_path: str | None = None,
        autonomy_level: int = 4,
        auto_submit: bool = False,
        overrides: dict[str, str] | None = None,
        cover_letter: str | None = None,
        cover_letter_path: str | None = None,
        keep_open: bool = False,
    ) -> RunResult:
        """Open, read, fill, verify — and stop unless explicitly cleared to submit.

        The default path never submits. Submission requires autonomy level 5,
        an explicit `auto_submit`, an allow-listed ATS, an empty verdict, and no
        unanswered required fields. Any one of those missing means a human
        decides.
        """
        page = await self._context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)

            if blocker := await self.detect_blocker(page):
                shot = await self.screenshot(page, f"blocked-{blocker}")
                return RunResult(
                    outcome=Outcome.HUMAN_REQUIRED,
                    url=url,
                    blocker=blocker,
                    screenshots=[shot] if shot else [],
                    error=f"{blocker} encountered — handing over to you",
                )

            spec, confidence = await self.read_form(page, url)
            result = await self.fill_form(
                page,
                spec,
                candidate,
                resume_path,
                overrides=overrides,
                cover_letter=cover_letter,
                cover_letter_path=cover_letter_path,
            )
            result.spec = spec
            result.confidence = confidence
            result.ats = spec.ats

            result.holds = await self.verify(page, spec)
            if shot := await self.screenshot(page, f"filled-{spec.ats}"):
                result.screenshots.append(shot)

            if result.needs_user:
                result.outcome = Outcome.NEEDS_USER
                return result
            if result.holds:
                result.outcome = Outcome.HELD
                return result

            cleared = (
                auto_submit
                and autonomy_level >= 5
                and may_auto_submit(spec.ats)
                and not spec.multi_step
            )
            if not cleared:
                result.outcome = Outcome.READY
                return result

            outcome, evidence = await self.submit(page, spec)
            result.outcome = outcome
            result.evidence = evidence
            if shot := await self.screenshot(page, f"submitted-{spec.ats}"):
                result.screenshots.append(shot)
            return result

        except Exception as exc:
            log.exception("apply_failed", url=url[:120])
            return RunResult(
                outcome=Outcome.FAILED, url=url, error=f"{type(exc).__name__}: {exc}"[:300]
            )
        finally:
            # A held page is the form the user actually reviewed. Closing it and
            # re-opening later would submit a different render of the page than the
            # one the screenshot shows, which is exactly the silent-wrong failure
            # the pre-submit verdict exists to prevent.
            if keep_open and not page.is_closed():
                self._held_page = page
            elif not page.is_closed():
                await page.close()
