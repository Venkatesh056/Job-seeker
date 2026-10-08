"use client";

/**
 * How a measurement is drawn.
 *
 * This is the component the whole interface is organised around. The backend never
 * reports a bare score: it reports a score *and* how much of it was actually
 * measured. Rendering only the first half is the visual equivalent of the bug this
 * system found three separate times — an unknown component contributing a zero and
 * nobody able to see the difference between "bad" and "unmeasured".
 *
 * So the bar has three regions and they are all visible:
 *
 *   ███████████░░░░░░░░⟍⟍⟍⟍⟍⟍
 *   known      headroom  unmeasured
 *
 * The hatched tail is the part nobody could measure. It is drawn rather than
 * omitted, because an empty track reads as zero and a shorter track reads as
 * certainty.
 */

import { useEffect, useRef, useState } from "react";

export function scoreTone(value: number): string {
  if (value >= 0.75) return "var(--color-verdant)";
  if (value >= 0.6) return "var(--color-signal)";
  if (value >= 0.4) return "var(--color-amber)";
  return "var(--color-rust)";
}

/**
 * The width comes from the caller, always.
 *
 * This carried `w-full` once, and in a narrow column it won the cascade against the
 * `w-20` a caller passed — the bar took the whole row and squeezed the label beside it
 * to zero characters. The row still had the company name in the DOM; it was simply
 * nowhere. `shrink-0` is the other half: in a flex row, a bar that can be compressed
 * is a measurement that changes size depending on how long the label next to it is.
 */
interface MeasureProps {
  /** 0–1. `null` means nobody measured this, which is different from measuring 0. */
  value: number | null;
  /** 0–1. How much of the score rests on evidence. Omit when the backend did not say. */
  confidence?: number | null;
  className?: string;
  height?: number;
  tone?: string;
}

export function Measure({
  value,
  confidence,
  className = "",
  height = 6,
  tone,
}: MeasureProps) {
  const [drawn, setDrawn] = useState(0);

  useEffect(() => {
    // Grow on mount. The animation is not decoration: a bar that arrives at its
    // final length shows you a number, and a bar that grows to it shows you a
    // measurement being taken.
    const id = requestAnimationFrame(() => setDrawn(value ?? 0));
    return () => cancelAnimationFrame(id);
  }, [value]);

  if (value === null || value === undefined) {
    return (
      <div
        className={`hatched shrink-0 rounded-full ${className}`}
        style={{ height }}
        title="not measured — which is not the same as zero"
      />
    );
  }

  // Confidence shortens the *solid* portion and leaves the remainder hatched, so a
  // high score on thin evidence looks visibly different from the same score on
  // thick evidence, without needing a second number beside it.
  const certainty = confidence ?? 1;
  const solid = drawn * certainty;
  const doubtful = drawn - solid;
  const colour = tone ?? scoreTone(value);

  return (
    <div
      className={`relative shrink-0 overflow-hidden rounded-full bg-ink-800 ${className}`}
      style={{ height }}
      title={
        confidence != null
          ? `${Math.round(value * 100)}%, ${Math.round(certainty * 100)}% of it from evidence`
          : `${Math.round(value * 100)}%`
      }
    >
      <div className="absolute inset-0 flex">
        <div
          className="h-full transition-[width] duration-700 ease-[cubic-bezier(0.22,1,0.36,1)]"
          style={{ width: `${solid * 100}%`, background: colour }}
        />
        {doubtful > 0.001 && (
          <div
            className="hatched h-full transition-[width] duration-700 ease-[cubic-bezier(0.22,1,0.36,1)]"
            style={{
              width: `${doubtful * 100}%`,
              backgroundImage: `repeating-linear-gradient(-45deg, ${colour} 0 2px, transparent 2px 6px)`,
              opacity: 0.55,
            }}
          />
        )}
      </div>
    </div>
  );
}

/** A number that counts up to itself. Grouped digits, tabular so it does not jitter. */
export function Ticker({
  value,
  duration = 900,
  className = "",
  suffix = "",
}: {
  value: number;
  duration?: number;
  className?: string;
  suffix?: string;
}) {
  const [shown, setShown] = useState(0);
  const from = useRef(0);

  useEffect(() => {
    const start = performance.now();
    const origin = from.current;
    let frame = 0;

    const step = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      // Quintic ease-out: fast enough to feel instant, slow enough at the end to
      // read the final digits settling.
      const eased = 1 - Math.pow(1 - t, 5);
      setShown(Math.round(origin + (value - origin) * eased));
      if (t < 1) frame = requestAnimationFrame(step);
      else from.current = value;
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [value, duration]);

  return (
    <span className={`tabular-nums ${className}`}>
      {shown.toLocaleString()}
      {suffix}
    </span>
  );
}

/**
 * A score as text, honest about not having one.
 *
 * An em dash, never "0%". The two mean opposite things and the dashboard that
 * prints the second when it means the first is the reason this component exists.
 */
export function Percent({
  value,
  className = "",
}: {
  value: number | null | undefined;
  className?: string;
}) {
  if (value === null || value === undefined) {
    return (
      <span className={`text-chalk-faint ${className}`} title="not measured">
        —
      </span>
    );
  }
  return (
    <span className={`tabular-nums ${className}`}>{Math.round(value * 100)}%</span>
  );
}
