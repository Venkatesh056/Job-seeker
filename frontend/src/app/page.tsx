"use client";

/**
 * The command centre.
 *
 * Arranged around one question — *what should I do next?* — rather than around the
 * data model. A job hunt produces a great many numbers and almost all of them are
 * retrospective; the two panels that earn the top of this page are the ones that
 * name a next action, and the counts sit underneath them.
 *
 * The funnel is the exception, and it earns its place by being the only view that
 * shows where the hunt is *stuck*. A number that has not moved in a week is the
 * finding.
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  api,
  type Funnel,
  type Job,
  type Status,
  type UncontactedResponse,
  type UntouchedCompany,
} from "@/lib/api";
import { Measure, Percent, Ticker } from "@/components/Measure";
import { Panel, Pill, Stat } from "@/components/Panel";

export default function Overview() {
  const [status, setStatus] = useState<Status | null>(null);
  const [funnel, setFunnel] = useState<Funnel | null>(null);
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [people, setPeople] = useState<UncontactedResponse | null>(null);
  const [companies, setCompanies] = useState<UntouchedCompany[] | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    api<Status>("/status").then(setStatus).catch((e) => setFailed(e.message));
    api<Funnel>("/analytics/funnel").then(setFunnel).catch(() => setFunnel(null));
    api<Job[]>("/jobs?limit=6").then(setJobs).catch(() => setJobs([]));
    api<UncontactedResponse>("/graph/questions/uncontacted-people?limit=5")
      .then(setPeople)
      .catch(() => setPeople(null));
    api<{ companies: UntouchedCompany[] }>(
      "/graph/questions/untouched-companies?limit=5",
    )
      .then((r) => setCompanies(r.companies))
      .catch(() => setCompanies([]));
  }, []);

  return (
    <div className="mx-auto max-w-[1280px] space-y-5">
      <Headline status={status} error={failed} />

      <div className="grid gap-5 lg:grid-cols-[1.1fr_1fr]">
        <NextMoves people={people} companies={companies} />
        <FunnelPanel funnel={funnel} />
      </div>

      <div className="grid gap-5 lg:grid-cols-[1.35fr_1fr]">
        <TopMatches jobs={jobs} />
        <Machinery status={status} />
      </div>
    </div>
  );
}

/* ─── Headline ──────────────────────────────────────────────────────────── */

function Headline({
  status,
  error,
}: {
  status: Status | null;
  error: string | null;
}) {
  if (error) {
    return (
      <Panel error={error}>
        <div />
      </Panel>
    );
  }

  return (
    <Panel delay={0}>
      <div className="flex flex-wrap items-end justify-between gap-6">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight">
            {status?.candidate ? (
              <>
                Good hunting,{" "}
                <span className="text-signal">
                  {status.candidate.split(" ")[0]}
                </span>
                .
              </>
            ) : (
              "No resume loaded yet"
            )}
          </h1>
          <p className="mt-1.5 max-w-[56ch] text-[12px] leading-relaxed text-chalk-dim">
            {status?.candidate ? (
              <>
                Every score below carries how much of it rests on evidence. A solid
                bar was measured; a hatched one was not, and that is different from
                being low.
              </>
            ) : (
              "Upload a CV to build the Fact Bank. Nothing is invented to fill a gap."
            )}
          </p>
        </div>

        <div className="flex gap-8">
          <Stat label="openings" hint="Deduplicated across every board">
            <Ticker value={status?.jobs ?? 0} />
          </Stat>
          <Stat label="boards" hint="ATS boards resolved and crawlable">
            <Ticker value={status?.boards ?? 0} />
          </Stat>
          <Stat
            label="facts verified"
            tone={
              status && status.verified_facts === 0 ? "text-amber" : "text-chalk"
            }
            hint="Facts you have confirmed, of those parsed from your CV"
          >
            <Ticker value={status?.verified_facts ?? 0} />
            <span className="text-chalk-faint">/{status?.facts ?? 0}</span>
          </Stat>
        </div>
      </div>
    </Panel>
  );
}

/* ─── What to do next ───────────────────────────────────────────────────── */

function NextMoves({
  people,
  companies,
}: {
  people: UncontactedResponse | null;
  companies: UntouchedCompany[] | null;
}) {
  const loading = people === null && companies === null;
  const nobody = people?.people.length === 0 && companies?.length === 0;

  return (
    <Panel
      title="Next moves"
      hint="People and employers with an opening you match, that have heard nothing from you"
      delay={60}
      loading={loading}
      empty={
        nobody
          ? people?.note ||
            "Nothing waiting. Either everyone relevant has been contacted, or no people have been discovered yet."
          : null
      }
    >
      <div className="space-y-4">
        {people && people.people.length > 0 && (
          <div>
            <div className="mb-2 flex items-center gap-2">
              <span className="font-mono text-[9px] uppercase tracking-[0.14em] text-chalk-faint">
                Write to
              </span>
              <span className="h-px flex-1 bg-ink-800" />
              <Link
                href="/people"
                className="text-[10px] text-chalk-faint transition-colors hover:text-signal"
              >
                all people →
              </Link>
            </div>
            <div className="space-y-1">
              {people.people.map((person) => (
                <Link
                  key={person.person_id}
                  href={`/people?focus=${encodeURIComponent(person.person_id)}`}
                  className="group flex items-center gap-3 rounded-lg px-2 py-2 transition-colors hover:bg-ink-850"
                >
                  <Avatar name={person.name} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-[13px]">{person.name}</span>
                      {person.hiring_authority && (
                        <Pill tone="info" title="Hires for the function you are targeting">
                          hires
                        </Pill>
                      )}
                    </div>
                    <div className="truncate text-[11px] text-chalk-faint">
                      {person.title} · {person.company}
                    </div>
                  </div>
                  <div className="w-16 shrink-0 text-right">
                    <div className="font-mono text-[12px] tabular-nums">
                      {person.relevance}
                    </div>
                    <div className="font-mono text-[9px] text-chalk-faint">
                      {person.reachable ? person.channel || "reachable" : "no channel"}
                    </div>
                  </div>
                </Link>
              ))}
            </div>
            {people.note && (
              <p className="mt-2 px-2 text-[10px] leading-relaxed text-chalk-faint">
                {people.note}
              </p>
            )}
          </div>
        )}

        {companies && companies.length > 0 && (
          <div>
            <div className="mb-2 flex items-center gap-2">
              <span className="font-mono text-[9px] uppercase tracking-[0.14em] text-chalk-faint">
                Never contacted
              </span>
              <span className="h-px flex-1 bg-ink-800" />
            </div>
            <div className="space-y-1">
              {companies.map((company) => (
                <div
                  key={company.company}
                  className="flex items-center gap-3 rounded-lg px-2 py-1.5"
                >
                  <span className="min-w-0 flex-1 truncate text-[13px]">
                    {company.company}
                  </span>
                  <span className="shrink-0 font-mono text-[10px] text-chalk-faint">
                    {company.open_roles} open
                  </span>
                  <div className="w-20 shrink-0">
                    <Measure value={company.best_score} className="w-20" height={5} />
                  </div>
                  <Percent
                    value={company.best_score}
                    className="w-9 shrink-0 text-right font-mono text-[11px]"
                  />
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </Panel>
  );
}

function Avatar({ name }: { name: string }) {
  const initials = name
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
  // Hue from the name, so the same person is the same colour everywhere without
  // storing anything. Fixed saturation and lightness keep it legible on dark.
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) | 0;
  const hue = Math.abs(hash) % 360;
  return (
    <div
      className="grid h-7 w-7 shrink-0 place-items-center rounded-full font-mono text-[10px] font-semibold"
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

/* ─── Funnel ────────────────────────────────────────────────────────────── */

function FunnelPanel({ funnel }: { funnel: Funnel | null }) {
  if (!funnel) {
    return (
      <Panel title="Pipeline" delay={120} loading>
        <div />
      </Panel>
    );
  }

  const top = Math.max(...funnel.stages.map((s) => s.count), 1);

  return (
    <Panel
      title="Pipeline"
      hint="Where the hunt is, and where it stops"
      delay={120}
    >
      <div className="space-y-2">
        {funnel.stages.map((stage, index) => {
          const width = Math.max((stage.count / top) * 100, stage.count > 0 ? 2 : 0);
          // The first stage nobody reaches. Not styled as an error — it is a
          // perfectly ordinary state early on — but worth the eye landing on it,
          // because "where does this stop" is the only question this chart answers.
          const isWall =
            stage.count === 0 &&
            index > 0 &&
            funnel.stages[index - 1].count > 0;
          return (
            <div key={stage.stage} className="group">
              <div className="mb-1 flex items-baseline gap-2">
                <span className="text-[11px] capitalize text-chalk-dim">
                  {stage.stage.replace(/_/g, " ")}
                </span>
                {isWall && (
                  <span className="font-mono text-[9px] text-amber">← stops here</span>
                )}
                <span className="ml-auto font-mono text-[12px] tabular-nums">
                  {stage.count.toLocaleString()}
                </span>
                {stage.from_previous !== null && (
                  <span className="w-11 text-right font-mono text-[10px] text-chalk-faint">
                    {Math.round(stage.from_previous * 100)}%
                  </span>
                )}
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-ink-800">
                <div
                  className="h-full rounded-full transition-[width] duration-700 ease-[cubic-bezier(0.22,1,0.36,1)]"
                  style={{
                    width: `${width}%`,
                    background: `linear-gradient(90deg, var(--color-signal-deep), var(--color-signal))`,
                    opacity: 1 - index * 0.06,
                  }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </Panel>
  );
}

/* ─── Top matches ───────────────────────────────────────────────────────── */

function TopMatches({ jobs }: { jobs: Job[] | null }) {
  return (
    <Panel
      title="Strongest matches"
      hint="Best scoring open roles right now"
      delay={180}
      loading={jobs === null}
      empty={jobs?.length === 0 ? "No scored openings yet. Run a discovery sweep." : null}
      action={
        <Link
          href="/jobs"
          className="text-[11px] text-chalk-faint transition-colors hover:text-signal"
        >
          all openings →
        </Link>
      }
    >
      <div className="space-y-1">
        {jobs?.map((job) => (
          <a
            key={job.id}
            href={job.urls[0]}
            target="_blank"
            rel="noreferrer"
            className="group flex items-center gap-3 rounded-lg px-2 py-2 transition-colors hover:bg-ink-850"
          >
            <div className="min-w-0 flex-1">
              <div className="truncate text-[13px] group-hover:text-signal">
                {job.title}
              </div>
              <div className="truncate text-[11px] text-chalk-faint">
                {job.company}
                {job.location ? ` · ${job.location}` : ""}
                {job.work_mode !== "unknown" ? ` · ${job.work_mode}` : ""}
              </div>
            </div>
            <div className="w-24 shrink-0">
              <Measure value={job.final_score} className="w-24" height={5} />
            </div>
            <Percent
              value={job.final_score}
              className="w-9 shrink-0 text-right font-mono text-[12px]"
            />
          </a>
        ))}
      </div>
    </Panel>
  );
}

/* ─── Machinery ─────────────────────────────────────────────────────────── */

/**
 * What the system is running on. Here rather than buried in settings because the
 * two questions it answers — "are my rate limits shared across workers" and "where
 * do screenshots live" — have wrong answers that stay invisible until they matter.
 */
function Machinery({ status }: { status: Status | null }) {
  if (!status) {
    return (
      <Panel title="Machinery" delay={240} loading>
        <div />
      </Panel>
    );
  }

  const infra = status.infrastructure;
  const rows: { label: string; value: string; tone?: "good" | "warn" }[] = [
    { label: "Database", value: status.database },
    {
      label: "Models",
      value: status.llm_available ? "reachable" : "no key configured",
      tone: status.llm_available ? "good" : "warn",
    },
    {
      label: "Semantic matching",
      value: status.semantic_matching ? "on" : "off — keyword only",
      tone: status.semantic_matching ? "good" : "warn",
    },
    { label: "Rate limits", value: infra?.rate_limits?.backend ?? "—" },
    { label: "Object store", value: infra?.object_store ?? "—" },
    {
      label: "Tracing",
      value: infra?.tracing?.enabled ? infra.tracing.backend : "off",
    },
    {
      label: "Accounts",
      value: infra?.multi_user ? "enforced" : "single user",
    },
  ];

  return (
    <Panel title="Machinery" hint="What this is actually running on" delay={240}>
      <dl className="space-y-2">
        {rows.map((row) => (
          <div key={row.label} className="flex items-baseline gap-3 text-[11px]">
            <dt className="w-32 shrink-0 text-chalk-faint">{row.label}</dt>
            <dd
              className={`min-w-0 flex-1 truncate font-mono ${
                row.tone === "warn"
                  ? "text-amber"
                  : row.tone === "good"
                    ? "text-verdant"
                    : "text-chalk-dim"
              }`}
              title={row.value}
            >
              {row.value}
            </dd>
          </div>
        ))}
      </dl>

      <div className="mt-4 rounded-lg border border-ink-700 bg-ink-900/60 px-3 py-2.5">
        <div className="font-mono text-[9px] uppercase tracking-[0.14em] text-chalk-faint">
          Autonomy level {status.autonomy_level}
        </div>
        <p className="mt-1 text-[11px] leading-relaxed text-chalk-dim">
          {status.autonomy_description}
        </p>
      </div>
    </Panel>
  );
}
