"use client";

/**
 * Openings.
 *
 * A table, because a job hunt is a comparison and comparison wants columns. The one
 * departure from a plain table is the score, which is drawn rather than printed —
 * ranking fifty roles by reading fifty percentages is work a bar does for free.
 *
 * Filtering happens in the browser. The corpus is a few hundred scored rows, which
 * is nothing to filter locally and a round trip to filter remotely; and a filter that
 * responds on the keystroke is a different tool from one that responds in 300ms.
 */

import { useEffect, useMemo, useState } from "react";
import { api, type Job } from "@/lib/api";
import { Measure, Percent } from "@/components/Measure";
import { Panel, Pill } from "@/components/Panel";

type Sort = "score" | "recent" | "company";

export default function JobsPage() {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<Sort>("score");
  const [remoteOnly, setRemoteOnly] = useState(false);
  const [minScore, setMinScore] = useState(0);

  useEffect(() => {
    api<Job[]>("/jobs?limit=200")
      .then(setJobs)
      .catch((e) => setError(e.message));
  }, []);

  const shown = useMemo(() => {
    if (!jobs) return [];
    const needle = query.trim().toLowerCase();
    const filtered = jobs.filter((job) => {
      if (remoteOnly && job.work_mode !== "remote") return false;
      if ((job.final_score ?? 0) < minScore) return false;
      if (!needle) return true;
      return `${job.title} ${job.company} ${job.location}`
        .toLowerCase()
        .includes(needle);
    });

    return filtered.sort((a, b) => {
      if (sort === "company") return a.company.localeCompare(b.company);
      if (sort === "recent") {
        // Undated postings sort last rather than first. An empty date is not
        // "today", and a board that stopped publishing dates would otherwise take
        // over the top of this list.
        if (!a.posted_date) return 1;
        if (!b.posted_date) return -1;
        return b.posted_date.localeCompare(a.posted_date);
      }
      return (b.final_score ?? -1) - (a.final_score ?? -1);
    });
  }, [jobs, query, sort, remoteOnly, minScore]);

  const unscored = jobs?.filter((j) => j.final_score === null).length ?? 0;

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
            placeholder="Filter by role, employer or city…"
            className="w-full rounded-lg border border-ink-700 bg-ink-900 py-2 pl-9 pr-3 text-[13px] outline-none transition-colors placeholder:text-chalk-faint focus:border-signal/50"
          />
        </div>

        <button
          onClick={() => setRemoteOnly((on) => !on)}
          className={`rounded-lg border px-3 py-2 text-[12px] transition-colors ${
            remoteOnly
              ? "border-signal/40 bg-signal/10 text-signal"
              : "border-ink-700 text-chalk-dim hover:border-ink-600"
          }`}
        >
          Remote only
        </button>

        <div className="flex items-center gap-2 rounded-lg border border-ink-700 bg-ink-900 px-3 py-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-chalk-faint">
            min
          </span>
          <input
            type="range"
            min={0}
            max={0.9}
            step={0.05}
            value={minScore}
            onChange={(event) => setMinScore(Number(event.target.value))}
            className="w-24 accent-[var(--color-signal)]"
          />
          <span className="w-8 font-mono text-[11px] tabular-nums">
            {Math.round(minScore * 100)}%
          </span>
        </div>

        <div className="flex rounded-lg border border-ink-700 p-0.5">
          {(["score", "recent", "company"] as Sort[]).map((option) => (
            <button
              key={option}
              onClick={() => setSort(option)}
              className={`rounded-md px-2.5 py-1.5 text-[12px] capitalize transition-colors ${
                sort === option ? "bg-ink-800 text-chalk" : "text-chalk-faint"
              }`}
            >
              {option}
            </button>
          ))}
        </div>
      </div>

      <Panel
        loading={jobs === null}
        error={error}
        empty={
          jobs && shown.length === 0
            ? jobs.length === 0
              ? "No openings yet. Run a discovery sweep to fill the corpus."
              : "Nothing matches those filters."
            : null
        }
        action={
          jobs && (
            <span className="font-mono text-[11px] text-chalk-faint">
              {shown.length.toLocaleString()} of {jobs.length.toLocaleString()}
              {unscored > 0 && ` · ${unscored} unscored`}
            </span>
          )
        }
      >
        <div className="-mx-1">
          {shown.slice(0, 200).map((job, index) => (
            <a
              key={job.id}
              href={job.urls[0]}
              target="_blank"
              rel="noreferrer"
              className="group grid grid-cols-[1fr_auto] items-center gap-4 rounded-lg px-3 py-2.5 transition-colors hover:bg-ink-850 md:grid-cols-[minmax(0,1fr)_160px_180px]"
              style={{ animationDelay: `${Math.min(index, 14) * 18}ms` }}
            >
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="truncate text-[13.5px] group-hover:text-signal">
                    {job.title}
                  </span>
                  {job.work_mode === "remote" && <Pill tone="info">remote</Pill>}
                </div>
                <div className="mt-0.5 truncate text-[11px] text-chalk-faint">
                  {job.company}
                  {job.location ? ` · ${job.location}` : ""}
                  {job.seniority !== "unknown" ? ` · ${job.seniority}` : ""}
                  {job.posted_date ? ` · ${job.posted_date}` : ""}
                </div>
              </div>

              <div className="hidden min-w-0 md:block">
                {job.missing_skills.length > 0 ? (
                  <div
                    className="truncate font-mono text-[10px] text-amber/80"
                    title={`Wants, and your Fact Bank does not name: ${job.missing_skills.join(", ")}`}
                  >
                    missing {job.missing_skills.slice(0, 2).join(", ")}
                    {job.missing_skills.length > 2 &&
                      ` +${job.missing_skills.length - 2}`}
                  </div>
                ) : (
                  <div className="font-mono text-[10px] text-chalk-faint">
                    {job.ats ?? ""}
                  </div>
                )}
              </div>

              <div className="flex items-center gap-3">
                <Measure value={job.final_score} className="w-24" height={5} />
                <Percent
                  value={job.final_score}
                  className="w-9 text-right font-mono text-[12.5px]"
                />
              </div>
            </a>
          ))}
        </div>

        {shown.length > 200 && (
          <p className="mt-3 text-center font-mono text-[10px] text-chalk-faint">
            showing the first 200 of {shown.length.toLocaleString()} — narrow the filter
          </p>
        )}
      </Panel>
    </div>
  );
}
