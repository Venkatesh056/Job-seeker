"use client";

/**
 * The career graph, as a thing you can push around.
 *
 * Hand-rolled rather than a library, for the same reason the old dashboard was: the
 * layout is a hundred lines of arithmetic, and a graph library is a megabyte of
 * someone else's opinions about how you should hold a mouse.
 *
 * The physics is a standard three-force model — links pull, every pair repels, and a
 * weak pull to centre keeps disconnected pieces on screen — cooled to a stop so it
 * settles rather than shivering forever. Two details that matter more than the
 * algorithm:
 *
 * **Everything is capped, and the cap is said out loud.** Past a few hundred nodes a
 * force layout is a grey disc. The backend truncates and reports it; this draws the
 * notice, because a capped graph that does not say so invites the reader to conclude
 * they have seen everything.
 *
 * **A node is not just a dot.** Kind decides colour, degree decides size. A hub with
 * eleven edges should look like a hub without anybody having to count.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type GraphEdge, type GraphNode, type GraphView } from "@/lib/api";
import { Panel, Pill } from "@/components/Panel";

interface Body extends GraphNode {
  x: number;
  y: number;
  vx: number;
  vy: number;
  degree: number;
  radius: number;
}

/**
 * Colour by kind. Warm hues for people, cool for structure, gold for you — the same
 * assignment the old dashboard used, so switching between them does not require
 * relearning the picture.
 */
const KIND_COLOUR: Record<string, string> = {
  candidate: "#e3c07b",
  skill: "#6ea8ff",
  job: "#55c57a",
  company: "#e0857f",
  application: "#a684e8",
  fact: "#8a8a8a",
  ats: "#4fc3c0",
  location: "#b0a07a",
  person: "#e0a4b0",
  role: "#c7b3d6",
  department: "#9fb8c9",
  outreach: "#e8b478",
  contact: "#e0a4b0",
  interview: "#8fd1c0",
  offer: "#7bd18f",
  lead: "#d6c08f",
  resume_version: "#b8b8d6",
};

const colourFor = (kind: string) => KIND_COLOUR[kind] ?? "#7a829c";

export default function GraphPage() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const bodiesRef = useRef<Body[]>([]);
  const edgesRef = useRef<GraphEdge[]>([]);
  const frameRef = useRef(0);
  const hoverRef = useRef<Body | null>(null);
  const rafRef = useRef(0);

  const [view, setView] = useState<GraphView | null>(null);
  const [root, setRoot] = useState<string | null>(null);
  const [depth, setDepth] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const [hovered, setHovered] = useState<Body | null>(null);
  const [trail, setTrail] = useState<{ id: string; label: string }[]>([]);

  /* ─── Load ────────────────────────────────────────────────────────────── */

  const seed = useCallback((data: GraphView) => {
    const degree = new Map<string, number>();
    for (const edge of data.edges) {
      degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
      degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
    }

    const count = Math.max(data.nodes.length, 1);
    bodiesRef.current = data.nodes.map((node, index) => {
      const d = degree.get(node.id) ?? 0;
      // Seeded on a ring rather than at random. A random start takes far longer to
      // untangle and the first second looks like a knot coming apart, which reads
      // as the software struggling.
      const angle = (index / count) * Math.PI * 2;
      const isRoot = node.attrs?.root === true || node.id === root;
      return {
        ...node,
        x: 480 + Math.cos(angle) * (isRoot ? 0 : 230) + Math.random() * 12,
        y: 300 + Math.sin(angle) * (isRoot ? 0 : 190) + Math.random() * 12,
        vx: 0,
        vy: 0,
        degree: d,
        radius: Math.min(16, 4.5 + Math.sqrt(d) * 2.1) + (isRoot ? 3 : 0),
      };
    });
    edgesRef.current = data.edges;
    frameRef.current = 0;
    // `root` is read to decide which node is the centre, so the callback has to be
    // rebuilt when it changes — otherwise a re-root would lay out around the old one.
  }, [root]);

  useEffect(() => {
    const path = root
      ? `/graph/around?node=${encodeURIComponent(root)}&depth=${depth}&max_nodes=220`
      : "/graph?max_nodes=260&jobs=36";

    let alive = true;
    api<GraphView>(path)
      .then((data) => {
        if (!alive) return;
        // Cleared here rather than at the top of the effect: setting state
        // synchronously as an effect opens cascades a render for no reason, and an
        // error from the previous view should stay on screen until this one answers.
        setError(null);
        setView(data);
        seed(data);
      })
      .catch((e) => alive && setError(e.message));
    return () => {
      alive = false;
    };
  }, [root, depth, seed]);

  /* ─── Simulate and draw ───────────────────────────────────────────────── */

  // The integrator mutates the bodies in place, and the React Compiler flags that:
  // values reachable from a ref are values it cannot prove nobody else is reading.
  // Here nobody is — `bodiesRef` is written by `seed` and read only by this loop —
  // and the alternative is allocating a fresh array of ~120 objects sixty times a
  // second to produce an identical picture. The rule is right in general and wrong
  // about this function, so it is turned off for this function and nowhere else.
  /* eslint-disable react-hooks/immutability */
  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (canvas.width !== width * ratio || canvas.height !== height * ratio) {
      canvas.width = width * ratio;
      canvas.height = height * ratio;
    }

    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);

    const bodies = bodiesRef.current;
    const edges = edgesRef.current;
    const index = new Map(bodies.map((b) => [b.id, b]));

    // Cool to a stop. Without this the layout jitters forever, which is both
    // distracting and a constant drain on a laptop battery.
    if (frameRef.current < 320) {
      const heat = 1 - frameRef.current / 320;

      for (const edge of edges) {
        const a = index.get(edge.source);
        const b = index.get(edge.target);
        if (!a || !b) continue;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const distance = Math.hypot(dx, dy) || 1;
        // Weight is how strongly the backend believes the link. A verified skill
        // sits closer to you than an inferred one, which makes belief visible as
        // distance rather than as a number nobody reads.
        const rest = 116 - edge.weight * 30;
        const force = ((distance - rest) / distance) * 0.038 * heat;
        a.vx += dx * force;
        a.vy += dy * force;
        b.vx -= dx * force;
        b.vy -= dy * force;
      }

      for (let i = 0; i < bodies.length; i++) {
        const a = bodies[i];
        for (let j = i + 1; j < bodies.length; j++) {
          const b = bodies[j];
          const dx = b.x - a.x;
          const dy = b.y - a.y;
          const squared = dx * dx + dy * dy || 1;
          if (squared > 115_600) continue; // beyond 340px, repulsion is noise
          // Tuned against the real corpus at ~120 nodes. At 1400 the core packed into
          // an unreadable clot with labels stacked on each other; the cost of too much
          // repulsion is only empty space, and the cost of too little is a grey disc.
          const push = (2300 / squared) * heat;
          const distance = Math.sqrt(squared);
          const ux = (dx / distance) * push;
          const uy = (dy / distance) * push;
          a.vx -= ux;
          a.vy -= uy;
          b.vx += ux;
          b.vy += uy;
        }
      }

      const cx = width / 2;
      const cy = height / 2;
      const margin = 26;
      for (const body of bodies) {
        body.vx += (cx - body.x) * 0.0022 * heat;
        body.vy += (cy - body.y) * 0.0022 * heat;

        // A soft wall, not a hard one. Clamping to the edge looked like a bug: the
        // low-degree nodes that repulsion pushes outward arrived at the boundary and
        // *stopped there*, in a neat row, which reads as a deliberate layout rather
        // than as a pile-up. A force that grows as it nears the edge turns them back
        // instead of parking them against it.
        const overLeft = margin - (body.x - body.radius);
        const overRight = body.x + body.radius - (width - margin);
        const overTop = margin - (body.y - body.radius);
        const overBottom = body.y + body.radius - (height - margin);
        if (overLeft > 0) body.vx += overLeft * 0.05;
        if (overRight > 0) body.vx -= overRight * 0.05;
        if (overTop > 0) body.vy += overTop * 0.05;
        if (overBottom > 0) body.vy -= overBottom * 0.05;

        body.vx *= 0.86;
        body.vy *= 0.86;
        body.x += body.vx;
        body.y += body.vy;

        // A last resort, well outside the soft wall, so nothing can escape the frame
        // entirely — a node drawn off-canvas is a node the reader concludes is absent.
        body.x = Math.max(body.radius + 2, Math.min(width - body.radius - 2, body.x));
        body.y = Math.max(body.radius + 2, Math.min(height - body.radius - 2, body.y));
      }
      frameRef.current++;
    }

    ctx.clearRect(0, 0, width, height);

    const hover = hoverRef.current;
    const lit = new Set<string>();
    if (hover) {
      lit.add(hover.id);
      for (const edge of edges) {
        if (edge.source === hover.id) lit.add(edge.target);
        if (edge.target === hover.id) lit.add(edge.source);
      }
    }

    for (const edge of edges) {
      const a = index.get(edge.source);
      const b = index.get(edge.target);
      if (!a || !b) continue;
      const active = hover ? lit.has(a.id) && lit.has(b.id) : false;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.strokeStyle = active
        ? "rgba(110,168,255,0.55)"
        : hover
          ? "rgba(120,130,160,0.07)"
          : "rgba(120,130,160,0.18)";
      ctx.lineWidth = active ? 1.4 : 1;
      ctx.stroke();
    }

    for (const body of bodies) {
      const dim = hover != null && !lit.has(body.id);
      const colour = colourFor(body.kind);

      if (!dim && (body.attrs?.root === true || body.id === hover?.id)) {
        ctx.beginPath();
        ctx.arc(body.x, body.y, body.radius + 6, 0, Math.PI * 2);
        ctx.fillStyle = `${colour}22`;
        ctx.fill();
      }

      ctx.beginPath();
      ctx.arc(body.x, body.y, body.radius, 0, Math.PI * 2);
      ctx.fillStyle = dim ? `${colour}33` : colour;
      ctx.fill();

      // Label the hubs and whatever is under the cursor. Labelling everything at
      // this density produces a wall of overlapping text that is less readable
      // than no labels at all.
      const worthLabelling = body.degree >= 5 || body.attrs?.root === true;
      if (!dim && (worthLabelling || body.id === hover?.id)) {
        ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
        ctx.fillStyle = body.id === hover?.id ? "#e8eaf2" : "rgba(232,234,242,0.62)";
        ctx.textAlign = "center";
        const label =
          body.label.length > 24 ? `${body.label.slice(0, 22)}…` : body.label;
        ctx.fillText(label, body.x, body.y + body.radius + 13);
      }
    }

  }, []);
  /* eslint-enable react-hooks/immutability */

  // The loop lives here rather than inside `draw` scheduling itself. A callback that
  // ends with `requestAnimationFrame(draw)` has to name itself before it exists, and
  // the compiler is right to object — the self-reference also keeps every frame's
  // closure alive, which is a slow leak nobody notices on a page left open all day.
  useEffect(() => {
    let running = true;
    const frame = () => {
      if (!running) return;
      draw();
      rafRef.current = requestAnimationFrame(frame);
    };
    rafRef.current = requestAnimationFrame(frame);
    return () => {
      running = false;
      cancelAnimationFrame(rafRef.current);
    };
  }, [draw]);

  /* ─── Pointer ─────────────────────────────────────────────────────────── */

  const bodyAt = (event: React.MouseEvent<HTMLCanvasElement>): Body | null => {
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    let best: Body | null = null;
    let bestDistance = Infinity;
    for (const body of bodiesRef.current) {
      const distance = Math.hypot(body.x - x, body.y - y);
      // A generous hit radius. Asking somebody to hit a five-pixel circle with a
      // mouse is asking them to stop using the feature.
      if (distance < body.radius + 9 && distance < bestDistance) {
        best = body;
        bestDistance = distance;
      }
    }
    return best;
  };

  const onMove = (event: React.MouseEvent<HTMLCanvasElement>) => {
    const body = bodyAt(event);
    hoverRef.current = body;
    setHovered(body);
    event.currentTarget.style.cursor = body ? "pointer" : "default";
  };

  const onClick = (event: React.MouseEvent<HTMLCanvasElement>) => {
    const body = bodyAt(event);
    if (!body) return;
    // Facts and locations are leaves. Re-rooting on one shows a single dot and a
    // line, which feels like the app broke.
    if (["fact", "location", "role", "department"].includes(body.kind)) return;
    setTrail((crumbs) => [...crumbs, { id: body.id, label: body.label }]);
    setRoot(body.id);
  };

  /* ─── Frame ───────────────────────────────────────────────────────────── */

  return (
    <div className="mx-auto max-w-[1280px] space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <button
          onClick={() => {
            setRoot(null);
            setTrail([]);
          }}
          className="rounded-lg border border-ink-700 bg-ink-850 px-3 py-1.5 text-[12px] transition-colors hover:border-ink-600"
        >
          ◈ You
        </button>
        {trail.map((crumb, index) => (
          <div key={`${crumb.id}-${index}`} className="flex items-center gap-2">
            <span className="text-chalk-faint">/</span>
            <button
              onClick={() => {
                setTrail(trail.slice(0, index + 1));
                setRoot(crumb.id);
              }}
              className={`rounded-lg px-2.5 py-1.5 text-[12px] transition-colors ${
                index === trail.length - 1
                  ? "bg-ink-800 text-chalk"
                  : "text-chalk-dim hover:text-chalk"
              }`}
            >
              {crumb.label}
            </button>
          </div>
        ))}

        <div className="ml-auto flex items-center gap-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-chalk-faint">
            depth
          </span>
          {[1, 2].map((d) => (
            <button
              key={d}
              onClick={() => setDepth(d)}
              disabled={!root}
              className={`rounded-md border px-2.5 py-1 font-mono text-[11px] transition-colors disabled:opacity-40 ${
                depth === d && root
                  ? "border-signal/40 bg-signal/10 text-signal"
                  : "border-ink-700 text-chalk-dim hover:border-ink-600"
              }`}
            >
              {d}
            </button>
          ))}
        </div>
      </div>

      <Panel
        error={error}
        className="overflow-hidden"
        action={
          view && (
            <div className="flex items-center gap-2">
              {view.truncated && (
                <Pill tone="warn" title="There is more than is drawn here">
                  capped
                </Pill>
              )}
              <span className="font-mono text-[11px] text-chalk-faint">
                {view.node_count} nodes · {view.edge_count} links
              </span>
            </div>
          )
        }
      >
        <div className="relative">
          <canvas
            ref={canvasRef}
            onMouseMove={onMove}
            onMouseLeave={() => {
              hoverRef.current = null;
              setHovered(null);
            }}
            onClick={onClick}
            className="block h-[540px] w-full rounded-lg"
          />

          {hovered && (
            <div className="pointer-events-none absolute left-3 top-3 max-w-[280px] rounded-lg border border-ink-600 bg-ink-950/92 px-3 py-2 backdrop-blur">
              <div className="flex items-center gap-2">
                <span
                  className="h-2 w-2 rounded-full"
                  style={{ background: colourFor(hovered.kind) }}
                />
                <span className="font-mono text-[9px] uppercase tracking-[0.12em] text-chalk-faint">
                  {hovered.kind.replace(/_/g, " ")}
                </span>
              </div>
              <div className="mt-1 text-[13px] leading-snug">{hovered.label}</div>
              <div className="mt-1 font-mono text-[10px] text-chalk-faint">
                {hovered.degree} connection{hovered.degree === 1 ? "" : "s"}
                {["fact", "location", "role", "department"].includes(hovered.kind)
                  ? ""
                  : " · click to re-centre"}
              </div>
            </div>
          )}

          <p className="absolute bottom-3 right-3 font-mono text-[10px] text-chalk-faint">
            a projection over the tables — nothing is stored here
          </p>
        </div>
      </Panel>

      {view && (
        <Panel title="What is in view" delay={80}>
          <div className="flex flex-wrap gap-x-5 gap-y-2">
            {Object.entries(view.counts)
              .sort((a, b) => b[1] - a[1])
              .map(([kind, count]) => (
                <div key={kind} className="flex items-center gap-2">
                  <span
                    className="h-2.5 w-2.5 rounded-full"
                    style={{ background: colourFor(kind) }}
                  />
                  <span className="text-[12px] capitalize text-chalk-dim">
                    {kind.replace(/_/g, " ")}
                  </span>
                  <span className="font-mono text-[12px] tabular-nums">{count}</span>
                </div>
              ))}
          </div>
        </Panel>
      )}
    </div>
  );
}
