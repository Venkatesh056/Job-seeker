"use client";

/**
 * The approval queue.
 *
 * Phase 3 left this as *"an approval screen showing recipient, source, relevance, message,
 * resume and prior contact in one place. The data is all there; the screen is not."* It was
 * worse than that — no endpoint could produce those six things together either, so
 * approving from the list view meant approving a subject line.
 *
 * That matters because of what approval **is** here: the human gate in front of an
 * irreversible act. A gate you pass without seeing anything is not a gate. So this screen
 * shows the whole message, always expanded, with everything that should change your mind
 * next to it:
 *
 *   who it reaches, and why they scored what they scored
 *   the URL their address was found at — not a claim that one exists
 *   what they have already had from you, across every channel
 *   which resume is attached
 *
 * **Approving does not send.** That is a separate, explicit call, and the button says so.
 */

import { useCallback, useEffect, useState } from "react";
import { api, post } from "@/lib/api";
import { Panel, Pill } from "@/components/Panel";

interface QueueItem {
  id: string;
  status: string;
  channel: string;
  company: string;
  created_at: string | null;
  recipient: {
    name: string;
    role: string;
    address: string;
    source: string;
    relevance: number | null;
    relevance_reasons: string[];
  };
  message: {
    subject: string;
    body: string;
    words: number;
    fact_ids: string[];
    resume_render_id: string | null;
  };
  prior_contact: {
    count: number;
    replied: boolean;
    last_at: string | null;
    channels: string[];
  };
  blocked: { reason: string; note: string } | null;
  deliverable?: { ready: boolean; why: string; fix: string };
}

export default function ApprovalsPage() {
  const [queue, setQueue] = useState<QueueItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    api<{ queue: QueueItem[]; note: string }>("/outreach/review?limit=30")
      .then((r) => setQueue(r.queue))
      .catch((e) => setError(e.message));
  }, []);

  useEffect(load, [load]);

  const act = async (id: string, what: "approve" | "cancel") => {
    setBusy(id);
    try {
      await post(`/outreach/${id}/${what}`);
      load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  /* An older API does not send `deliverable`. Treated as "not ready, and I cannot tell
     you why" rather than assumed sendable — a stale server should disable the approve
     button, not hand out a green one. A missing field crashed this page once, which is
     the worse failure of the two. */
  const readiness = (item: QueueItem) =>
    item.deliverable ?? {
      ready: false,
      why: "this server did not report whether the message can be sent",
      fix: "restart the API",
    };

  const blocked = queue?.filter((q) => q.blocked).length ?? 0;
  const ready = queue?.filter((q) => readiness(q).ready).length ?? 0;

  return (
    <div className="mx-auto max-w-[900px] space-y-4">
      <div className="panel panel-lit rise-in flex flex-wrap items-center gap-x-8 gap-y-3 px-4 py-3">
        <Figure label="awaiting you" value={queue?.length ?? 0} />
        <Figure label="ready to send" value={ready} />
        <Figure
          label="blocked"
          value={blocked}
          tone={blocked ? "text-rust" : "text-chalk"}
        />
        <p className="ml-auto max-w-[46ch] text-[11px] leading-relaxed text-chalk-faint">
          Approving marks a draft ready. It does not send it — sending is a separate,
          explicit step, and the transport checks the do-not-contact list again on its way
          out.
        </p>
      </div>

      <Panel
        loading={queue === null}
        error={error}
        empty={
          queue?.length === 0
            ? "Nothing waiting. Draft a message from the People screen."
            : null
        }
      >
        <div className="space-y-4">
          {queue?.map((item) => (
            <article
              key={item.id}
              className={`rounded-xl border px-4 py-3.5 ${
                item.blocked
                  ? "border-rust/30 bg-rust/[0.04]"
                  : "border-ink-700 bg-ink-900/50"
              }`}
            >
              {/* ── Who ─────────────────────────────────────────────── */}
              <header className="flex flex-wrap items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[14px]">
                      {item.recipient.name || (
                        <span className="text-chalk-faint">no recipient yet</span>
                      )}
                    </span>
                    <Pill tone={item.channel === "email" ? "info" : "default"}>
                      {item.channel}
                    </Pill>
                    {item.recipient.relevance !== null && (
                      <Pill tone="good" title="Career-aware relevance for your target role">
                        {item.recipient.relevance}/100
                      </Pill>
                    )}
                    {item.prior_contact.count > 0 && (
                      <Pill
                        tone="warn"
                        title={`Already contacted ${item.prior_contact.count} time(s) via ${item.prior_contact.channels.join(", ")}`}
                      >
                        contacted before
                      </Pill>
                    )}
                  </div>
                  <div className="mt-1 text-[11px] text-chalk-faint">
                    {[item.recipient.role, item.company].filter(Boolean).join(" · ")}
                    {item.recipient.address ? ` · ${item.recipient.address}` : ""}
                  </div>
                </div>

                <div className="flex shrink-0 gap-2">
                  <button
                    onClick={() => void act(item.id, "cancel")}
                    disabled={busy === item.id}
                    className="rounded-lg border border-ink-700 px-3 py-1.5 text-[12px] text-chalk-dim transition-colors hover:border-rust/40 hover:text-rust disabled:opacity-50"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={() => void act(item.id, "approve")}
                    disabled={busy === item.id || !readiness(item).ready}
                    title={
                      readiness(item).ready
                        ? "Marks it ready. Does not send."
                        : readiness(item).why
                    }
                    className="rounded-lg border border-verdant/40 bg-verdant/10 px-3 py-1.5 text-[12px] text-verdant transition-colors hover:bg-verdant/20 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {item.status === "approved" ? "Approved" : "Approve"}
                  </button>
                </div>
              </header>

              {/* ── Why it cannot go ────────────────────────────────── */}
              {!readiness(item).ready && (
                <p
                  className={`mt-2.5 rounded-lg border px-3 py-2 text-[11px] leading-relaxed ${
                    item.blocked
                      ? "border-rust/30 bg-rust/[0.06] text-rust"
                      : "border-amber/25 bg-amber/[0.05] text-amber"
                  }`}
                >
                  {readiness(item).why}
                  {readiness(item).fix && (
                    <span className="text-chalk-faint"> — {readiness(item).fix}</span>
                  )}
                </p>
              )}

              {/* ── The message, in full ────────────────────────────── */}
              <div className="mt-3 rounded-lg border border-ink-800 bg-ink-950/50 px-3 py-2.5">
                {item.message.subject && (
                  <div className="mb-1.5 border-b border-ink-800 pb-1.5 text-[12px]">
                    <span className="font-mono text-[9px] uppercase tracking-[0.12em] text-chalk-faint">
                      subject{" "}
                    </span>
                    {item.message.subject}
                  </div>
                )}
                {/* Never truncated. A reviewer approving the first two lines of a message
                    is approving a message they have not read. */}
                <p className="whitespace-pre-wrap text-[12.5px] leading-relaxed text-chalk-dim">
                  {item.message.body}
                </p>
              </div>

              {/* ── What backs it ───────────────────────────────────── */}
              <footer className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5 font-mono text-[10px] text-chalk-faint">
                <span>{item.message.words} words</span>
                <span title="Claims traceable to a confirmed fact in your Fact Bank">
                  {item.message.fact_ids.length} facts cited
                </span>
                <span>
                  {item.message.resume_render_id
                    ? `resume ${item.message.resume_render_id.slice(0, 8)}`
                    : "no resume attached"}
                </span>
                {item.recipient.source && (
                  <a
                    href={item.recipient.source}
                    target="_blank"
                    rel="noreferrer"
                    className="truncate text-signal hover:underline"
                    title="The page this address was found on — check it rather than trust it"
                  >
                    found at {new URL(item.recipient.source).hostname} ↗
                  </a>
                )}
              </footer>

              {item.recipient.relevance_reasons.length > 0 && (
                <details className="mt-2">
                  <summary className="cursor-pointer text-[11px] text-chalk-faint hover:text-chalk-dim">
                    Why this person
                  </summary>
                  <ul className="mt-1.5 space-y-1 pl-3">
                    {item.recipient.relevance_reasons.map((reason, index) => (
                      <li key={index} className="text-[11px] leading-relaxed text-chalk-dim">
                        · {reason}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </article>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function Figure({
  label,
  value,
  tone = "text-chalk",
}: {
  label: string;
  value: number;
  tone?: string;
}) {
  return (
    <div>
      <div className={`text-[20px] font-semibold leading-none tabular-nums ${tone}`}>
        {value}
      </div>
      <div className="mt-1.5 font-mono text-[9px] uppercase tracking-[0.12em] text-chalk-faint">
        {label}
      </div>
    </div>
  );
}
