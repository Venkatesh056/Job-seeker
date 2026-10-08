"use client";

/**
 * The surfaces everything else is built out of.
 *
 * `Panel` carries one rule that is easy to skip and expensive to skip: it
 * distinguishes **loading**, **failed**, and **genuinely empty**. Most dashboards
 * collapse all three into a blank rectangle, and a blank rectangle is read as "there
 * is nothing here" — so a backend that is down looks identical to a job hunt that
 * has not started, and the reader draws the wrong conclusion from a true picture.
 */

import type { ReactNode } from "react";

export function Panel({
  title,
  hint,
  action,
  children,
  className = "",
  loading = false,
  error = null,
  empty = null,
  delay = 0,
}: {
  title?: string;
  hint?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  loading?: boolean;
  error?: string | null;
  /** Shown instead of children when there is genuinely nothing — say *why*. */
  empty?: string | null;
  /** Stagger, in ms. Panels arriving together read as one page; in sequence they read as an interface being assembled. */
  delay?: number;
}) {
  return (
    <section
      className={`panel panel-lit rise-in ${className}`}
      style={delay ? { animationDelay: `${delay}ms` } : undefined}
    >
      {(title || action) && (
        <header className="flex items-center gap-3 border-b border-ink-800 px-4 py-3">
          <div className="min-w-0">
            {title && (
              <h2 className="truncate text-[13px] font-semibold tracking-tight">
                {title}
              </h2>
            )}
            {hint && (
              <p className="mt-0.5 truncate text-[11px] text-chalk-faint">{hint}</p>
            )}
          </div>
          {action && <div className="ml-auto shrink-0">{action}</div>}
        </header>
      )}

      <div className="p-4">
        {loading ? (
          <Skeleton />
        ) : error ? (
          <Failed message={error} />
        ) : empty ? (
          <p className="py-6 text-center text-[12px] text-chalk-faint">{empty}</p>
        ) : (
          children
        )}
      </div>
    </section>
  );
}

/**
 * A shimmer, not a spinner. A spinner says "something is happening somewhere"; a
 * shimmer in the shape of the content says "the thing you asked for is arriving and
 * it will be about this big".
 */
function Skeleton() {
  return (
    <div className="space-y-2.5">
      {[0, 1, 2].map((row) => (
        <div
          key={row}
          className="relative h-8 overflow-hidden rounded-md bg-ink-800/60"
        >
          <div
            className="absolute inset-0 -translate-x-full bg-gradient-to-r from-transparent via-white/[0.04] to-transparent"
            style={{
              animation: "sweep 1.4s ease-in-out infinite",
              animationDelay: `${row * 0.12}s`,
            }}
          />
        </div>
      ))}
    </div>
  );
}

/** Says what went wrong. An error with no detail sends somebody to the logs. */
function Failed({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-2.5 rounded-lg border border-rust/25 bg-rust/[0.06] px-3 py-2.5">
      <span className="mt-px text-rust">⚠</span>
      <p className="text-[12px] leading-relaxed text-chalk-dim">{message}</p>
    </div>
  );
}

/** A small labelled statistic. Used in rows of three or four. */
export function Stat({
  label,
  children,
  tone = "text-chalk",
  hint,
}: {
  label: string;
  children: ReactNode;
  tone?: string;
  hint?: string;
}) {
  return (
    <div title={hint}>
      <div className={`text-[26px] font-semibold leading-none tracking-tight ${tone}`}>
        {children}
      </div>
      <div className="mt-2 font-mono text-[10px] uppercase tracking-[0.12em] text-chalk-faint">
        {label}
      </div>
    </div>
  );
}

export function Pill({
  children,
  tone = "default",
  title,
}: {
  children: ReactNode;
  tone?: "default" | "good" | "warn" | "bad" | "info";
  title?: string;
}) {
  const tones: Record<string, string> = {
    default: "border-ink-600 text-chalk-dim",
    good: "border-verdant/35 text-verdant bg-verdant/[0.07]",
    warn: "border-amber/35 text-amber bg-amber/[0.07]",
    bad: "border-rust/35 text-rust bg-rust/[0.07]",
    info: "border-signal/35 text-signal bg-signal/[0.07]",
  };
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.08em] ${tones[tone]}`}
    >
      {children}
    </span>
  );
}
