"use client";

/**
 * The Fact Bank, and the confirm step it was designed around.
 *
 * `Fact.verified` has carried the same comment since the first migration — *flipped by
 * human review, never by the parser; unverified facts are usable for matching but not for
 * generated resume claims.* There was no human review anywhere in the system. No endpoint
 * set the flag and the facts listing did not even return an id, so 0 of 49 verified was
 * not neglect, it was unreachable.
 *
 * This screen is that review, and it is built for the only thing that matters about it:
 * **getting through forty-nine of them without it feeling like a chore.** Keyboard first,
 * one line each, evidence locator visible so you can check the claim against the page it
 * came from.
 *
 * What it will not do is confirm anything for you. There is no "verify all" button here
 * and no such parameter on the endpoint. This flag is what lets a claim reach a resume
 * sent to an employer; a single click that sets it across the table would make the whole
 * Fact Bank ceremonial.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, post } from "@/lib/api";
import { Panel, Pill } from "@/components/Panel";

interface Fact {
  id: string;
  kind: string;
  label: string;
  body: string;
  evidence_source: string;
  evidence_locator: string;
  strength: number;
  verified: boolean;
}

const KIND_TONE: Record<string, "info" | "good" | "warn" | "default"> = {
  skill: "info",
  project: "good",
  experience: "good",
  education: "default",
  certification: "warn",
  achievement: "warn",
  link: "default",
  preference: "default",
};

export default function FactsPage() {
  const [facts, setFacts] = useState<Fact[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [cursor, setCursor] = useState(0);
  const [onlyUnverified, setOnlyUnverified] = useState(false);

  const load = useCallback(() => {
    api<Fact[]>("/candidate/facts")
      .then(setFacts)
      .catch((e) => setError(e.message));
  }, []);

  useEffect(load, [load]);

  const shown = useMemo(
    () => (facts ?? []).filter((f) => !onlyUnverified || !f.verified),
    [facts, onlyUnverified],
  );

  const verified = facts?.filter((f) => f.verified).length ?? 0;
  const total = facts?.length ?? 0;

  const review = useCallback(
    async (ids: string[], value: boolean) => {
      if (!ids.length) return;
      setSaving(true);
      // Optimistic, then reconciled by the reload below. Forty-nine round trips that
      // each repaint on arrival would make the keyboard flow feel like wading.
      setFacts((current) =>
        (current ?? []).map((f) =>
          ids.includes(f.id) ? { ...f, verified: value } : f,
        ),
      );
      try {
        await post("/candidate/facts/verify", { fact_ids: ids, verified: value });
      } catch (e) {
        setError((e as Error).message);
        load();
      } finally {
        setSaving(false);
      }
    },
    [load],
  );

  /* ─── Keyboard ──────────────────────────────────────────────────────────
     j/k or arrows to move, space or enter to toggle. The whole point is that
     forty-nine facts should take two minutes, and reaching for a mouse forty-nine
     times is what makes it take fifteen. */
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA"].includes(target.tagName)) return;

      if (event.key === "j" || event.key === "ArrowDown") {
        event.preventDefault();
        setCursor((c) => Math.min(c + 1, shown.length - 1));
      } else if (event.key === "k" || event.key === "ArrowUp") {
        event.preventDefault();
        setCursor((c) => Math.max(c - 1, 0));
      } else if (event.key === " " || event.key === "Enter") {
        const fact = shown[cursor];
        if (!fact) return;
        event.preventDefault();
        void review([fact.id], !fact.verified);
        // Step on after confirming, so holding space walks the list. Not after
        // *un*confirming — withdrawing a claim is a deliberate act and moving the
        // cursor off it makes the next space land somewhere unintended.
        if (!fact.verified) setCursor((c) => Math.min(c + 1, shown.length - 1));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [shown, cursor, review]);

  const unverifiedIds = shown.filter((f) => !f.verified).map((f) => f.id);

  return (
    <div className="mx-auto max-w-[1000px] space-y-4">
      <div className="panel panel-lit rise-in flex flex-wrap items-center gap-x-8 gap-y-3 px-4 py-3">
        <div>
          <div
            className={`text-[20px] font-semibold leading-none tabular-nums ${verified === 0 ? "text-amber" : "text-chalk"}`}
          >
            {verified}
            <span className="text-chalk-faint">/{total}</span>
          </div>
          <div className="mt-1.5 font-mono text-[9px] uppercase tracking-[0.12em] text-chalk-faint">
            confirmed
          </div>
        </div>

        <button
          onClick={() => setOnlyUnverified((on) => !on)}
          className={`rounded-lg border px-3 py-2 text-[12px] transition-colors ${
            onlyUnverified
              ? "border-signal/40 bg-signal/10 text-signal"
              : "border-ink-700 text-chalk-dim hover:border-ink-600"
          }`}
        >
          Only unconfirmed
        </button>

        <p className="ml-auto max-w-[44ch] text-[11px] leading-relaxed text-chalk-faint">
          A confirmed fact may appear in a resume sent to an employer. An unconfirmed one
          is used for matching and never quoted. Read each line before you confirm it.
        </p>
      </div>

      <Panel
        title="Facts parsed from your CV"
        hint="j / k to move · space to confirm · nothing is confirmed for you"
        loading={facts === null}
        error={error}
        empty={
          facts && shown.length === 0
            ? total === 0
              ? "No resume parsed yet."
              : "Everything here is confirmed."
            : null
        }
        action={
          unverifiedIds.length > 0 ? (
            <span className="font-mono text-[11px] text-chalk-faint">
              {saving ? "saving…" : `${unverifiedIds.length} to review`}
            </span>
          ) : null
        }
      >
        <div className="-mx-1">
          {shown.map((fact, index) => (
            <div
              key={fact.id}
              onMouseEnter={() => setCursor(index)}
              className={`flex items-start gap-3 rounded-lg px-3 py-2 transition-colors ${
                index === cursor ? "bg-ink-850" : ""
              }`}
            >
              <button
                onClick={() => void review([fact.id], !fact.verified)}
                aria-label={fact.verified ? "withdraw confirmation" : "confirm"}
                className={`mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded border transition-colors ${
                  fact.verified
                    ? "border-verdant bg-verdant/20 text-verdant"
                    : "border-ink-600 text-transparent hover:border-chalk-faint"
                }`}
              >
                <span className="text-[10px] leading-none">✓</span>
              </button>

              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    className={`text-[13px] ${fact.verified ? "" : "text-chalk-dim"}`}
                  >
                    {fact.label}
                  </span>
                  <Pill tone={KIND_TONE[fact.kind] ?? "default"}>{fact.kind}</Pill>
                </div>
                {fact.body && (
                  <p className="mt-0.5 line-clamp-2 text-[11px] leading-relaxed text-chalk-faint">
                    {fact.body}
                  </p>
                )}
              </div>

              <span
                className="hidden shrink-0 font-mono text-[10px] text-chalk-faint sm:block"
                title="Where in your CV this came from — check the claim against it"
              >
                {fact.evidence_locator}
              </span>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}
