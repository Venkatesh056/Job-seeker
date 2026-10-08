"use client";

/**
 * Companies.
 *
 * Employers, how many openings each has, and how well the best of them fits. The
 * research state is shown explicitly rather than implied by a blank cell, because
 * *"nobody has researched this company"* and *"this company scored badly"* are
 * different findings that a missing number cannot distinguish.
 */

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { api, type Job } from "@/lib/api";
import { Measure, Percent } from "@/components/Measure";
import { Panel, Pill } from "@/components/Panel";

interface CompanyRow {
  name: string;
  openings: number;
  best: number | null;
  ats: string | null;
  locations: Set<string>;
  remote: number;
}

export default function CompaniesPage() {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    api<Job[]>("/jobs?limit=200")
      .then(setJobs)
      .catch((e) => setError(e.message));
  }, []);

  // Rolled up from the openings rather than fetched separately. The company table
  // holds the 66 employers that have been *researched*; the openings name several
  // hundred, and a page called "Companies" that showed the smaller number would be
  // quietly answering a different question than the one asked.
  const rows = useMemo(() => {
    if (!jobs) return [];
    const byName = new Map<string, CompanyRow>();
    for (const job of jobs) {
      if (!job.company) continue;
      const row = byName.get(job.company) ?? {
        name: job.company,
        openings: 0,
        best: null,
        ats: job.ats,
        locations: new Set<string>(),
        remote: 0,
      };
      row.openings += 1;
      if (job.final_score !== null) {
        row.best = row.best === null ? job.final_score : Math.max(row.best, job.final_score);
      }
      if (job.location) row.locations.add(job.location);
      if (job.work_mode === "remote") row.remote += 1;
      byName.set(job.company, row);
    }

    const needle = query.trim().toLowerCase();
    return [...byName.values()]
      .filter((row) => !needle || row.name.toLowerCase().includes(needle))
      .sort((a, b) => (b.best ?? -1) - (a.best ?? -1) || b.openings - a.openings);
  }, [jobs, query]);

  return (
    <div className="mx-auto max-w-[1100px] space-y-4">
      <div className="relative">
        <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-chalk-faint">
          ⌕
        </span>
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Filter employers…"
          className="w-full rounded-lg border border-ink-700 bg-ink-900 py-2 pl-9 pr-3 text-[13px] outline-none transition-colors placeholder:text-chalk-faint focus:border-signal/50"
        />
      </div>

      <Panel
        loading={jobs === null}
        error={error}
        empty={
          jobs && rows.length === 0
            ? jobs.length === 0
              ? "No openings yet, so no employers to roll up."
              : "No employer matches that."
            : null
        }
        action={
          <span className="font-mono text-[11px] text-chalk-faint">
            {rows.length.toLocaleString()} employers
          </span>
        }
      >
        <div className="-mx-1">
          {rows.slice(0, 120).map((row) => (
            <div
              key={row.name}
              className="flex items-center gap-3 rounded-lg px-3 py-2.5 transition-colors hover:bg-ink-850"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-[13.5px]">{row.name}</span>
                  {row.remote > 0 && (
                    <Pill tone="info">{row.remote} remote</Pill>
                  )}
                </div>
                <div className="mt-0.5 truncate text-[11px] text-chalk-faint">
                  {row.openings} opening{row.openings === 1 ? "" : "s"}
                  {row.ats ? ` · ${row.ats}` : ""}
                  {row.locations.size
                    ? ` · ${[...row.locations].slice(0, 2).join(", ")}`
                    : ""}
                </div>
              </div>

              <Link
                href={`/graph`}
                className="hidden shrink-0 text-[11px] text-chalk-faint transition-colors hover:text-signal sm:block"
              >
                graph →
              </Link>

              <div className="flex w-[140px] shrink-0 items-center gap-3">
                <Measure value={row.best} className="w-20" height={5} />
                <Percent
                  value={row.best}
                  className="w-9 text-right font-mono text-[12.5px]"
                />
              </div>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}
