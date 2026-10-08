"use client";

/**
 * The frame everything sits in: a rail on the left, a status strip along the top,
 * and a command palette over the whole thing.
 *
 * The rail is grouped by *what you are doing*, not by which table the data lives in.
 * "Find work", "Reach people", "Understand" — a job hunt has phases, and a nav that
 * mirrors the schema instead of the phases makes you translate every time you look
 * at it.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api, type Status } from "@/lib/api";
import { CommandPalette } from "./CommandPalette";

export interface NavItem {
  href: string;
  label: string;
  glyph: string;
  hint: string;
}

export const NAV: { group: string; items: NavItem[] }[] = [
  {
    group: "Overview",
    items: [
      { href: "/", label: "Command", glyph: "◈", hint: "Everything at a glance" },
      { href: "/facts", label: "Fact Bank", glyph: "✓", hint: "What you can truthfully claim" },
    ],
  },
  {
    group: "Find work",
    items: [
      { href: "/jobs", label: "Openings", glyph: "▤", hint: "Scored roles" },
      { href: "/companies", label: "Companies", glyph: "⬢", hint: "Employers and boards" },
    ],
  },
  {
    group: "Reach people",
    items: [
      { href: "/people", label: "People", glyph: "◉", hint: "Who to write to" },
      { href: "/approvals", label: "Approvals", glyph: "⚖", hint: "Read before you send" },
      { href: "/outreach", label: "Outreach", glyph: "✉", hint: "Drafts and sends" },
    ],
  },
  {
    group: "Understand",
    items: [
      { href: "/graph", label: "Graph", glyph: "⌘", hint: "How it all connects" },
      { href: "/insights", label: "Insights", glyph: "◇", hint: "Named questions" },
    ],
  },
  {
    group: "System",
    items: [
      { href: "/keys", label: "Keys", glyph: "⚿", hint: "Providers and quota" },
    ],
  },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [status, setStatus] = useState<Status | null>(null);
  const [reachable, setReachable] = useState<boolean | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api<Status>("/status")
        .then((s) => {
          if (!alive) return;
          setStatus(s);
          setReachable(true);
        })
        .catch(() => alive && setReachable(false));
    load();
    // Slow on purpose. The status strip is ambient information; polling it hard
    // would put a request every second into the log of a system whose whole point
    // is an auditable record of what it did.
    const timer = setInterval(load, 20_000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
      }
      if (event.key === "Escape") setPaletteOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="relative z-10 flex min-h-screen">
      <aside className="sticky top-0 hidden h-screen w-[232px] shrink-0 flex-col border-r border-ink-800 bg-ink-950/70 backdrop-blur-xl md:flex">
        <div className="flex h-16 items-center gap-2.5 px-5">
          <Mark />
          <div className="leading-none">
            <div className="text-[13px] font-semibold tracking-tight">Career Agent</div>
            <div className="mt-1 font-mono text-[10px] text-chalk-faint">
              {status ? `v${status.version}` : "connecting…"}
            </div>
          </div>
        </div>

        <nav className="flex-1 overflow-y-auto px-3 pb-4">
          {NAV.map((section) => (
            <div key={section.group} className="mb-5">
              <div className="px-3 pb-2 font-mono text-[10px] uppercase tracking-[0.14em] text-chalk-faint">
                {section.group}
              </div>
              {section.items.map((item) => {
                const active =
                  item.href === "/"
                    ? pathname === "/"
                    : pathname.startsWith(item.href);
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={`group relative flex items-center gap-3 rounded-lg px-3 py-2 text-[13px] transition-colors ${
                      active
                        ? "bg-ink-800 text-chalk"
                        : "text-chalk-dim hover:bg-ink-850 hover:text-chalk"
                    }`}
                  >
                    {active && (
                      <span className="absolute left-0 top-1/2 h-4 w-[2px] -translate-y-1/2 rounded-r bg-signal" />
                    )}
                    <span
                      className={`w-4 text-center text-[13px] ${active ? "text-signal" : "text-chalk-faint group-hover:text-chalk-dim"}`}
                    >
                      {item.glyph}
                    </span>
                    {item.label}
                  </Link>
                );
              })}
            </div>
          ))}
        </nav>

        <button
          onClick={() => setPaletteOpen(true)}
          className="mx-3 mb-4 flex items-center justify-between rounded-lg border border-ink-700 bg-ink-900 px-3 py-2 text-[12px] text-chalk-faint transition-colors hover:border-ink-600 hover:text-chalk-dim"
        >
          Search
          <kbd className="rounded border border-ink-600 bg-ink-850 px-1.5 py-0.5 font-mono text-[10px]">
            ⌘K
          </kbd>
        </button>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-16 items-center gap-4 border-b border-ink-800 bg-ink-950/80 px-5 backdrop-blur-xl md:px-8">
          <Breadcrumb pathname={pathname} />
          <div className="ml-auto flex items-center gap-4">
            {status && <Vitals status={status} />}
            <Health reachable={reachable} />
          </div>
        </header>

        <main className="min-w-0 flex-1 px-5 py-7 md:px-8">{children}</main>
      </div>

      {/* Mounted only while open, so it starts empty every time without an effect
          that resets its state — and so it is not fetching in the background of every
          page view. */}
      {paletteOpen && <CommandPalette onClose={() => setPaletteOpen(false)} />}
    </div>
  );
}

function Mark() {
  return (
    <div className="relative grid h-8 w-8 place-items-center rounded-lg bg-gradient-to-br from-signal to-violet">
      <span className="text-[15px] font-bold text-ink-950">◈</span>
      <div className="absolute inset-0 rounded-lg ring-1 ring-inset ring-white/20" />
    </div>
  );
}

function Breadcrumb({ pathname }: { pathname: string }) {
  const item = NAV.flatMap((s) => s.items).find((i) =>
    i.href === "/" ? pathname === "/" : pathname.startsWith(i.href),
  );
  if (!item) return <div />;
  return (
    <div className="min-w-0">
      <div className="truncate text-[15px] font-semibold tracking-tight">
        {item.label}
      </div>
      <div className="truncate text-[11px] text-chalk-faint">{item.hint}</div>
    </div>
  );
}

/** Three numbers worth having on screen at all times, and nothing else. */
function Vitals({ status }: { status: Status }) {
  return (
    <div className="hidden items-center gap-5 lg:flex">
      <Vital label="jobs" value={status.jobs.toLocaleString()} />
      <Vital label="facts" value={`${status.verified_facts}/${status.facts}`} />
      <Vital
        label="autonomy"
        value={`L${status.autonomy_level}`}
        title={status.autonomy_description}
      />
    </div>
  );
}

function Vital({
  label,
  value,
  title,
}: {
  label: string;
  value: string;
  title?: string;
}) {
  return (
    <div className="text-right" title={title}>
      <div className="font-mono text-[13px] tabular-nums leading-none">{value}</div>
      <div className="mt-1 font-mono text-[9px] uppercase tracking-[0.12em] text-chalk-faint">
        {label}
      </div>
    </div>
  );
}

/**
 * Three states, not two. "Checking" is a real state and showing it as "down" for
 * the first second of every page load is how a healthy system gets a reputation
 * for being flaky.
 */
function Health({ reachable }: { reachable: boolean | null }) {
  const [tone, text] =
    reachable === null
      ? ["bg-chalk-faint", "checking"]
      : reachable
        ? ["bg-verdant", "live"]
        : ["bg-rust", "API down"];
  return (
    <div className="flex items-center gap-2">
      <span
        className={`h-1.5 w-1.5 rounded-full ${tone} ${reachable ? "live-dot" : ""}`}
      />
      <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-chalk-faint">
        {text}
      </span>
    </div>
  );
}
