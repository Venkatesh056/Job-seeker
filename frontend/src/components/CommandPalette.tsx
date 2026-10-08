"use client";

/**
 * ⌘K. Navigation and search in one box.
 *
 * It searches *across* things rather than within one — jobs, people and companies in
 * one list — because "Karya" is a company, a set of openings and eight people, and
 * having to decide which of those you meant before you can type it is the friction
 * this removes.
 *
 * Deliberately not a fuzzy matcher. Substring, case-insensitive, and ranked by where
 * the match lands. Fuzzy matching is a delight on a fixed command list and a menace
 * on live data — it finds *something* for every query, so a typo returns a confident
 * wrong answer rather than nothing.
 */

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Job, type PeopleResponse } from "@/lib/api";
import { NAV } from "./Shell";

interface Hit {
  id: string;
  title: string;
  subtitle: string;
  group: string;
  glyph: string;
  href: string;
  score: number;
}

export function CommandPalette({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [people, setPeople] = useState<PeopleResponse["people"]>([]);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();

    // Loaded once per opening, not per keystroke. The corpus is a few hundred rows
    // and filtering it locally is instant; a request per character would be slower
    // *and* would put a search in the audit log for every letter typed.
    let alive = true;
    Promise.all([
      api<Job[]>("/jobs?limit=200").catch(() => []),
      api<PeopleResponse>("/people?limit=300").catch(() => ({
        people: [],
        count: 0,
        contactable: 0,
        note: "",
      })),
    ]).then(([j, p]) => {
      if (!alive) return;
      setJobs(j);
      setPeople(p.people);
    });
    return () => {
      alive = false;
    };
  }, []);

  const hits = useMemo<Hit[]>(() => {
    const needle = query.trim().toLowerCase();

    const rank = (text: string): number => {
      const haystack = text.toLowerCase();
      const at = haystack.indexOf(needle);
      if (at < 0) return -1;
      // A match at the start beats one in the middle; a short label beats a long
      // one. Two rules, and they put "Karya" above "Senior Engineer at Karya".
      return 1000 - at * 10 - haystack.length;
    };

    const pages: Hit[] = NAV.flatMap((section) =>
      section.items.map((item) => ({
        id: `page:${item.href}`,
        title: item.label,
        subtitle: item.hint,
        group: "Go to",
        glyph: item.glyph,
        href: item.href,
        score: needle ? rank(`${item.label} ${item.hint}`) : 500,
      })),
    );

    if (!needle) return pages.filter((h) => h.score >= 0);

    const jobHits: Hit[] = jobs.map((job) => ({
      id: `job:${job.id}`,
      title: job.title,
      subtitle: `${job.company}${job.location ? ` · ${job.location}` : ""}`,
      group: "Openings",
      glyph: "▤",
      href: `/jobs?focus=${encodeURIComponent(job.id)}`,
      score: Math.max(rank(job.title), rank(job.company)),
    }));

    const peopleHits: Hit[] = people.map((person) => ({
      id: `person:${person.id}`,
      title: person.name,
      subtitle: `${person.title || person.function} · ${person.company}`,
      group: "People",
      glyph: "◉",
      href: `/people?focus=${encodeURIComponent(person.id)}`,
      score: Math.max(rank(person.name), rank(person.company), rank(person.title)),
    }));

    return [...pages, ...jobHits, ...peopleHits]
      .filter((hit) => hit.score >= 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, 24);
  }, [query, jobs, people]);

  const go = (hit: Hit) => {
    router.push(hit.href);
    onClose();
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setCursor((c) => Math.min(c + 1, hits.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setCursor((c) => Math.max(c - 1, 0));
    } else if (event.key === "Enter" && hits[cursor]) {
      event.preventDefault();
      go(hits[cursor]);
    }
  };

  // Precomputed rather than tracked with a mutable variable during the map. A
  // `lastGroup` reassigned mid-render is exactly the pattern that breaks when React
  // renders a component twice, and it breaks by dropping headings — quietly.
  const withHeaders = hits.map((hit, index) => ({
    hit,
    index,
    header: index === 0 || hits[index - 1].group !== hit.group ? hit.group : null,
  }));

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-ink-950/70 px-4 pt-[12vh] backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="panel panel-lit w-full max-w-[560px] overflow-hidden shadow-2xl shadow-black/60"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center gap-3 border-b border-ink-700 px-4">
          <span className="text-chalk-faint">⌕</span>
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              // Reset the selection here rather than in an effect watching `query`.
              // An effect would run a second render after every keystroke to do what
              // the keystroke already knew.
              setCursor(0);
            }}
            onKeyDown={onKeyDown}
            placeholder="Jump to a page, a role, or a person…"
            className="w-full bg-transparent py-3.5 text-[14px] outline-none placeholder:text-chalk-faint"
          />
          <kbd className="rounded border border-ink-600 bg-ink-850 px-1.5 py-0.5 font-mono text-[10px] text-chalk-faint">
            esc
          </kbd>
        </div>

        <div className="max-h-[52vh] overflow-y-auto py-2">
          {hits.length === 0 && (
            <div className="px-4 py-8 text-center text-[13px] text-chalk-faint">
              Nothing matches {query ? `“${query}”` : "that"}.
            </div>
          )}
          {withHeaders.map(({ hit, index, header }) => {
            return (
              <div key={hit.id}>
                {header && (
                  <div className="px-4 pb-1 pt-3 font-mono text-[9px] uppercase tracking-[0.14em] text-chalk-faint">
                    {header}
                  </div>
                )}
                <button
                  onMouseEnter={() => setCursor(index)}
                  onClick={() => go(hit)}
                  className={`flex w-full items-center gap-3 px-4 py-2 text-left transition-colors ${
                    index === cursor ? "bg-ink-800" : ""
                  }`}
                >
                  <span className="w-4 text-center text-[12px] text-chalk-faint">
                    {hit.glyph}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px]">{hit.title}</span>
                    <span className="block truncate text-[11px] text-chalk-faint">
                      {hit.subtitle}
                    </span>
                  </span>
                  {index === cursor && (
                    <span className="font-mono text-[10px] text-chalk-faint">↵</span>
                  )}
                </button>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
