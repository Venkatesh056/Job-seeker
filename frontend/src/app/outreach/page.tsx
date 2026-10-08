"use client";

/**
 * Outreach.
 *
 * Read-only, and that is a decision rather than an omission. Every irreversible step
 * in this system — approving a draft, sending it, marking something applied — goes
 * through an explicit action with a confirmation, and the old dashboard already
 * implements those flows against endpoints that were designed around them.
 *
 * Rebuilding a *send* button is not a migration, it is a second place where a message
 * can go out by accident. So this screen shows the state — what is drafted, what was
 * sent, who must never be written to — and hands off to the existing screen for the
 * actions. When those flows are rebuilt here properly, they replace that link.
 */

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Panel, Pill } from "@/components/Panel";

interface Suppressed {
  reason: string;
  target: string;
  note: string;
  at: string;
}

interface Limits {
  sent_today?: number;
  daily_cap?: number;
  remaining?: number;
  [key: string]: unknown;
}

interface OutreachRow {
  id: string;
  company?: string;
  subject?: string;
  status: string;
  channel?: string;
  created_at?: string;
  sent_at?: string | null;
}

const STATUS_TONE: Record<string, "good" | "warn" | "info" | "bad" | "default"> = {
  sent: "good",
  delivered: "good",
  replied: "good",
  approved: "info",
  awaiting_approval: "warn",
  drafted: "default",
  cancelled: "default",
  bounced: "bad",
  declined: "bad",
};

export default function OutreachPage() {
  const [messages, setMessages] = useState<OutreachRow[] | null>(null);
  const [suppressed, setSuppressed] = useState<Suppressed[] | null>(null);
  const [limits, setLimits] = useState<Limits | null>(null);
  const [gmail, setGmail] = useState<{ status?: string; note?: string } | null>(null);

  useEffect(() => {
    api<OutreachRow[] | { outreach: OutreachRow[] }>("/outreach")
      .then((d) => setMessages(Array.isArray(d) ? d : (d.outreach ?? [])))
      .catch(() => setMessages([]));
    api<Suppressed[] | { suppressed: Suppressed[] }>("/outreach/suppressed")
      .then((d) => setSuppressed(Array.isArray(d) ? d : (d.suppressed ?? [])))
      .catch(() => setSuppressed([]));
    api<Limits>("/outreach/limits")
      .then(setLimits)
      .catch(() => setLimits(null));
    api<{ status?: string; note?: string }>("/outreach/gmail/status")
      .then(setGmail)
      .catch(() => setGmail(null));
  }, []);

  return (
    <div className="mx-auto max-w-[1100px] space-y-5">
      <div className="panel panel-lit rise-in flex flex-wrap items-center gap-x-8 gap-y-3 px-4 py-3">
        <Figure label="messages" value={messages?.length ?? 0} />
        <Figure
          label="sent today"
          value={Number(limits?.sent_today ?? 0)}
          suffix={limits?.daily_cap ? ` / ${limits.daily_cap}` : ""}
        />
        <Figure
          label="do not contact"
          value={suppressed?.length ?? 0}
          tone={suppressed?.length ? "text-amber" : "text-chalk"}
        />
        <div className="ml-auto flex items-center gap-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-chalk-faint">
            gmail
          </span>
          <Pill
            tone={gmail?.status === "ok" ? "good" : "warn"}
            title={gmail?.note ?? "Send scope only — this system cannot read your mail"}
          >
            {gmail?.status === "ok" ? "connected" : "needs consent"}
          </Pill>
        </div>
      </div>

      <Panel
        title="Messages"
        hint="Nothing here was sent without an explicit approval. A draft is not a message."
        loading={messages === null}
        empty={
          messages?.length === 0
            ? "Nothing drafted yet. Find somebody worth writing to on the People screen."
            : null
        }
      >
        <div className="-mx-1">
          {messages?.map((message) => (
            <div
              key={message.id}
              className="flex items-center gap-3 rounded-lg px-3 py-2.5 transition-colors hover:bg-ink-850"
            >
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px]">
                  {message.subject || `${message.channel ?? "message"} to ${message.company ?? "—"}`}
                </div>
                <div className="mt-0.5 truncate text-[11px] text-chalk-faint">
                  {message.company}
                  {message.channel ? ` · ${message.channel}` : ""}
                  {message.sent_at ? ` · sent ${message.sent_at.slice(0, 10)}` : ""}
                </div>
              </div>
              <Pill tone={STATUS_TONE[message.status] ?? "default"}>
                {message.status.replace(/_/g, " ")}
              </Pill>
            </div>
          ))}
        </div>
      </Panel>

      <Panel
        title="Do not contact"
        hint="Absolute, and checked at three separate boundaries before anything can be sent."
        loading={suppressed === null}
        empty={suppressed?.length === 0 ? "Nobody has asked to be left alone." : null}
        delay={80}
      >
        <div className="space-y-1">
          {suppressed?.map((entry, index) => (
            <div
              key={`${entry.target}-${index}`}
              className="flex items-center gap-3 rounded-lg px-2 py-2"
            >
              <span className="min-w-0 flex-1 truncate font-mono text-[12px]">
                {entry.target}
              </span>
              {entry.note && (
                <span className="hidden truncate text-[11px] text-chalk-faint sm:block">
                  {entry.note}
                </span>
              )}
              <Pill tone={entry.reason === "opted_out" ? "bad" : "warn"}>
                {entry.reason.replace(/_/g, " ")}
              </Pill>
            </div>
          ))}
        </div>
      </Panel>

      <Panel delay={120}>
        <div className="flex flex-wrap items-center gap-3">
          <div className="min-w-0 flex-1">
            <div className="text-[13px]">Composing and sending</div>
            <p className="mt-1 max-w-[62ch] text-[11px] leading-relaxed text-chalk-faint">
              Drafting, approving and sending still live on the original dashboard.
              Those flows end in an irreversible action, and a second send button built
              in a hurry is a second way for a message to go out by accident — so they
              move when they can be moved carefully, not as part of a visual migration.
            </p>
          </div>
          <a
            href="http://127.0.0.1:8000/#outreach"
            target="_blank"
            rel="noreferrer"
            className="shrink-0 rounded-lg border border-ink-700 bg-ink-850 px-3 py-2 text-[12px] transition-colors hover:border-signal/40 hover:text-signal"
          >
            Open compose ↗
          </a>
        </div>
      </Panel>
    </div>
  );
}

function Figure({
  label,
  value,
  suffix = "",
  tone = "text-chalk",
}: {
  label: string;
  value: number;
  suffix?: string;
  tone?: string;
}) {
  return (
    <div>
      <div className={`text-[20px] font-semibold leading-none tabular-nums ${tone}`}>
        {value.toLocaleString()}
        <span className="text-chalk-faint">{suffix}</span>
      </div>
      <div className="mt-1.5 font-mono text-[9px] uppercase tracking-[0.12em] text-chalk-faint">
        {label}
      </div>
    </div>
  );
}
