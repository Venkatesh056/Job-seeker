"use client";

/**
 * People.
 *
 * The screen where the system's most important restraint has to be visible: **0 of
 * these 91 people have a published email, and none was guessed.** A contact detail
 * here cannot exist without the URL it was found at — that is enforced in the model,
 * not in a review step — so this page distinguishes three states that a lesser
 * interface would collapse into one green tick:
 *
 *   contactable   a verified, published address — the system may send
 *   reachable     a profile a human can open — you send, by hand
 *   no channel    nothing was found, and nothing will be invented
 *
 * Relevance is career-aware and carries a confidence, so it is drawn with the same
 * hatching as every other measurement. An engineering manager at 96 and a CEO at 12
 * for the same role is the ranking working, not a bug.
 */

import { useEffect, useMemo, useState } from "react";
import { api, type PeopleResponse, type Person } from "@/lib/api";
import { Measure } from "@/components/Measure";
import { Panel, Pill } from "@/components/Panel";

export default function PeoplePage() {
  const [data, setData] = useState<PeopleResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [worthwhile, setWorthwhile] = useState(true);

  useEffect(() => {
    api<PeopleResponse>("/people?limit=300")
      .then(setData)
      .catch((e) => setError(e.message));
  }, []);

  const shown = useMemo(() => {
    if (!data) return [];
    const needle = query.trim().toLowerCase();
    return data.people
      .filter((person) => {
        if (worthwhile && person.relevance < 45) return false;
        if (!needle) return true;
        return `${person.name} ${person.title} ${person.company} ${person.function}`
          .toLowerCase()
          .includes(needle);
      })
      .sort((a, b) => b.relevance - a.relevance);
  }, [data, query, worthwhile]);

  const published = data?.people.filter((p) => p.email).length ?? 0;

  return (
    <div className="mx-auto max-w-[1280px] space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[220px] flex-1">
          <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-chalk-faint">
            ⌕
          </span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Filter by name, title, employer or function…"
            className="w-full rounded-lg border border-ink-700 bg-ink-900 py-2 pl-9 pr-3 text-[13px] outline-none transition-colors placeholder:text-chalk-faint focus:border-signal/50"
          />
        </div>
        <button
          onClick={() => setWorthwhile((on) => !on)}
          className={`rounded-lg border px-3 py-2 text-[12px] transition-colors ${
            worthwhile
              ? "border-signal/40 bg-signal/10 text-signal"
              : "border-ink-700 text-chalk-dim hover:border-ink-600"
          }`}
          title="Relevance 45 and above — the threshold below which writing is usually noise"
        >
          Worth contacting
        </button>
      </div>

      {data && (
        <div className="panel panel-lit rise-in flex flex-wrap items-center gap-x-8 gap-y-3 px-4 py-3">
          <Figure label="discovered" value={data.count} />
          <Figure label="worth contacting" value={shown.length} />
          <Figure
            label="published emails"
            value={published}
            tone={published === 0 ? "text-chalk-dim" : "text-chalk"}
          />
          <p className="ml-auto max-w-[46ch] text-[11px] leading-relaxed text-chalk-faint">
            No address here was guessed. A contact detail cannot exist in this system
            without the page it was found on.
          </p>
        </div>
      )}

      <Panel
        loading={data === null}
        error={error}
        empty={
          data && shown.length === 0
            ? data.count === 0
              ? "Nobody discovered yet. Research a company to find its people."
              : "Nobody matches those filters."
            : null
        }
      >
        <div className="-mx-1">
          {shown.map((person) => (
            <PersonRow key={person.id} person={person} />
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
        {value.toLocaleString()}
      </div>
      <div className="mt-1.5 font-mono text-[9px] uppercase tracking-[0.12em] text-chalk-faint">
        {label}
      </div>
    </div>
  );
}

function PersonRow({ person }: { person: Person }) {
  const [open, setOpen] = useState(false);

  // Three states, not two. "Reachable" and "contactable" are different claims and
  // the difference is who does the sending.
  const contactable = Boolean(person.email);
  const reachable = contactable || Boolean(person.linkedin_url);

  return (
    <div className="rounded-lg transition-colors hover:bg-ink-850">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-3 px-3 py-2.5 text-left"
      >
        <Avatar name={person.name} />

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-[13.5px]">{person.name}</span>
            {person.hiring_authority && (
              <Pill tone="info" title="Hires for the function you are targeting">
                hires
              </Pill>
            )}
          </div>
          <div className="mt-0.5 truncate text-[11px] text-chalk-faint">
            {person.title || person.function} · {person.company}
            {person.location ? ` · ${person.location}` : ""}
          </div>
        </div>

        <div className="hidden w-[130px] shrink-0 sm:block">
          {contactable ? (
            <Pill tone="good" title="A verified published address — the system may send">
              contactable
            </Pill>
          ) : reachable ? (
            <Pill title="A profile you can open — you send, by hand">
              {person.recommended_channel || "reachable"}
            </Pill>
          ) : (
            <span className="font-mono text-[10px] text-chalk-faint">no channel</span>
          )}
        </div>

        <div className="flex w-[120px] shrink-0 items-center gap-2.5">
          <Measure
            value={person.relevance / 100}
            confidence={person.relevance_confidence || null}
            className="w-16"
            height={5}
          />
          <span className="w-6 text-right font-mono text-[12.5px] tabular-nums">
            {person.relevance}
          </span>
        </div>

        <span
          className={`shrink-0 text-chalk-faint transition-transform ${open ? "rotate-90" : ""}`}
        >
          ›
        </span>
      </button>

      {open && (
        <div className="border-t border-ink-800 px-3 py-3 pl-[52px]">
          <div className="font-mono text-[9px] uppercase tracking-[0.12em] text-chalk-faint">
            Why this score
          </div>
          <ul className="mt-1.5 space-y-1">
            {person.relevance_reasons.length ? (
              person.relevance_reasons.map((reason, index) => (
                <li
                  key={index}
                  className="flex gap-2 text-[12px] leading-relaxed text-chalk-dim"
                >
                  <span className="text-chalk-faint">·</span>
                  {reason}
                </li>
              ))
            ) : (
              <li className="text-[12px] text-chalk-faint">
                No reasons were recorded for this score.
              </li>
            )}
          </ul>
          {person.relevance_confidence > 0 && person.relevance_confidence < 1 && (
            <p className="mt-2 text-[11px] text-chalk-faint">
              {Math.round(person.relevance_confidence * 100)}% of this score rests on
              evidence; the hatched part of the bar is what could not be measured.
            </p>
          )}
          {person.linkedin_url && (
            <a
              href={person.linkedin_url}
              target="_blank"
              rel="noreferrer"
              className="mt-2.5 inline-block text-[12px] text-signal hover:underline"
            >
              Open profile ↗
            </a>
          )}
        </div>
      )}
    </div>
  );
}

function Avatar({ name }: { name: string }) {
  const initials = name
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) | 0;
  const hue = Math.abs(hash) % 360;
  return (
    <div
      className="grid h-8 w-8 shrink-0 place-items-center rounded-full font-mono text-[11px] font-semibold"
      style={{
        background: `hsl(${hue} 42% 22%)`,
        color: `hsl(${hue} 70% 76%)`,
        boxShadow: `inset 0 0 0 1px hsl(${hue} 40% 34%)`,
      }}
    >
      {initials}
    </div>
  );
}
