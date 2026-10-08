/**
 * The two components that encode this project's central rule.
 *
 * These tests exist because a regression here is **invisible**. A bar that renders solid
 * when it should be hatched looks completely fine — it just quietly claims certainty the
 * data does not support, which is the exact bug the backend found three separate times and
 * spent a whole design principle avoiding.
 *
 * So every assertion below is about a distinction, not about an appearance:
 *
 *   not measured   ≠   measured as zero
 *   thin evidence  ≠   thick evidence
 *   loading        ≠   failed        ≠   genuinely empty
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Measure, Percent, Ticker } from "./Measure";
import { Panel } from "./Panel";

describe("Measure", () => {
  it("draws a hatched track for a value nobody measured", () => {
    const { container } = render(<Measure value={null} />);
    const bar = container.firstElementChild!;

    expect(bar.className).toContain("hatched");
    // And it says so on hover, because the visual distinction is subtle by design.
    expect(bar.getAttribute("title")).toMatch(/not measured/i);
    expect(bar.getAttribute("title")).toMatch(/not the same as zero/i);
  });

  it("draws a real, empty track for a value measured as zero", () => {
    const { container } = render(<Measure value={0} />);
    const bar = container.firstElementChild!;

    // The opposite of the case above, and the whole point: zero was *measured*.
    expect(bar.className).not.toContain("hatched");
    expect(bar.getAttribute("title")).toBe("0%");
  });

  it("shortens the solid portion in proportion to confidence", () => {
    const { container } = render(<Measure value={0.8} confidence={0.5} />);
    // Traversed rather than selected. `div > div > div` also matched the wrapper
    // testing-library renders into, so the first "segment" was the bar itself.
    const track = container.firstElementChild!.firstElementChild!;
    const [solid, doubtful] = Array.from(track.children) as HTMLElement[];

    // 80% scored, half of it from evidence: 40% solid, 40% hatched.
    expect(solid.style.width).toBe("40%");
    expect(doubtful).toBeTruthy();
    expect(doubtful.style.width).toBe("40%");
    expect(doubtful.className).toContain("hatched");
  });

  it("draws no hatching when the whole score rests on evidence", () => {
    const { container } = render(<Measure value={0.8} confidence={1} />);
    const track = container.firstElementChild!.firstElementChild!;

    expect(track.children).toHaveLength(1);
    expect(track.children[0].className).not.toContain("hatched");
  });

  it("says how much of the score came from evidence", () => {
    const { container } = render(<Measure value={0.9} confidence={0.25} />);
    expect(container.firstElementChild!.getAttribute("title")).toBe(
      "90%, 25% of it from evidence",
    );
  });

  it("takes its width from the caller", () => {
    // It carried `w-full` once, which won the cascade against the `w-20` a caller passed
    // and squeezed the label beside it to zero characters. The row still had the company
    // name in the DOM; it was simply nowhere.
    const { container } = render(<Measure value={0.5} className="w-20" />);
    const bar = container.firstElementChild!;

    expect(bar.className).toContain("w-20");
    expect(bar.className).not.toContain("w-full");
    // And it must not be compressible, or a measurement changes size depending on how
    // long the text next to it is.
    expect(bar.className).toContain("shrink-0");
  });
});

describe("Percent", () => {
  it("prints an em dash for a value nobody measured", () => {
    render(<Percent value={null} />);
    // Never "0%". The two mean opposite things.
    expect(screen.getByTitle("not measured")).toHaveTextContent("—");
  });

  it("prints 0% for a value measured as zero", () => {
    const { container } = render(<Percent value={0} />);
    expect(container.textContent).toBe("0%");
  });

  it("rounds rather than truncating", () => {
    const { container } = render(<Percent value={0.567} />);
    expect(container.textContent).toBe("57%");
  });
});

describe("Ticker", () => {
  it("arrives at its value", () => {
    const { container } = render(<Ticker value={1748} />);
    expect(container.textContent).toBe("1,748");
  });
});

describe("Panel", () => {
  it("distinguishes loading from empty", () => {
    const { container } = render(
      <Panel loading>
        <p>content</p>
      </Panel>,
    );
    expect(container.textContent).not.toContain("content");
    // A shimmer in the shape of the content, not a spinner: it says what is arriving and
    // roughly how big it will be.
    expect(container.querySelectorAll(".bg-ink-800\\/60").length).toBeGreaterThan(0);
  });

  it("shows the reason a request failed rather than an empty box", () => {
    render(
      <Panel error="Cannot reach the API at http://127.0.0.1:8000/api/v1.">
        <p>content</p>
      </Panel>,
    );
    // The distinction that matters: a backend that is down must not look like a job hunt
    // that has not started.
    expect(screen.getByText(/Cannot reach the API/)).toBeInTheDocument();
    expect(screen.queryByText("content")).not.toBeInTheDocument();
  });

  it("says why it is empty when it genuinely is", () => {
    render(
      <Panel empty="No openings yet. Run a discovery sweep.">
        <p>content</p>
      </Panel>,
    );
    expect(screen.getByText(/Run a discovery sweep/)).toBeInTheDocument();
    expect(screen.queryByText("content")).not.toBeInTheDocument();
  });

  it("renders its children when there is something to show", () => {
    render(
      <Panel title="Openings">
        <p>content</p>
      </Panel>,
    );
    expect(screen.getByText("content")).toBeInTheDocument();
    expect(screen.getByText("Openings")).toBeInTheDocument();
  });

  it("prefers the error over the empty message", () => {
    // Both can be set by a component that reset its data and recorded a failure. The
    // failure is the more useful thing to say.
    render(
      <Panel error="the API is down" empty="nothing here">
        <p>content</p>
      </Panel>,
    );
    expect(screen.getByText("the API is down")).toBeInTheDocument();
    expect(screen.queryByText("nothing here")).not.toBeInTheDocument();
  });
});
