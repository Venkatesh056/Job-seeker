import "@testing-library/jest-dom/vitest";

/* jsdom has no frame loop, and two components here depend on one: `Measure` grows its bar
   from a single requestAnimationFrame, and `Ticker` counts up across many.

   Running callbacks synchronously is enough for the first and wrong for the second — a
   naive shim recurses forever, because `Ticker` loops until elapsed time reaches its
   duration and a synchronous loop never lets the clock move.

   So the shim advances a virtual clock by one frame each time it runs a callback, and
   `performance.now` reads that clock. Animations then complete immediately *and*
   terminate, which is what a test wants to assert on: the final state, with no timer to
   wait for. */
let virtualNow = 0;
const FRAME_MS = 16;

globalThis.requestAnimationFrame = ((callback: FrameRequestCallback) => {
  virtualNow += FRAME_MS;
  callback(virtualNow);
  return virtualNow;
}) as typeof globalThis.requestAnimationFrame;

globalThis.cancelAnimationFrame = (() => {}) as typeof globalThis.cancelAnimationFrame;

const realPerformanceNow = performance.now.bind(performance);
performance.now = () => virtualNow || realPerformanceNow();
