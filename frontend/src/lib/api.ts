/**
 * The one place that talks to the backend.
 *
 * Every type here was read off a live response rather than written from the Python
 * models — a hand-copied interface drifts the first time somebody adds a field, and
 * the drift shows up as `undefined` in a table cell rather than as an error.
 *
 * Two things this module refuses to do:
 *
 * - **Invent a number.** A missing value stays `null` all the way to the component,
 *   which decides how to render "we do not know". Defaulting to `0` here would
 *   quietly turn every unmeasured score into a bad one, which is the exact bug the
 *   backend spent three rounds removing.
 * - **Swallow a failure.** A failed request throws with what the server said, so a
 *   panel can show the reason instead of an empty state that looks like "no data".
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000/api/v1";

const TOKEN_KEY = "careeragent_token";

export function getToken(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(value: string): void {
  if (typeof window === "undefined") return;
  try {
    if (value) window.localStorage.setItem(TOKEN_KEY, value);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private window, or storage disabled — the session lasts this page load */
  }
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const headers = new Headers(init?.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init?.body && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  } catch {
    // The backend not running is the single most likely failure in development,
    // and "Failed to fetch" tells nobody anything.
    throw new ApiError(
      `Cannot reach the API at ${API_BASE}. Is the backend running?`,
      0,
    );
  }

  const text = await response.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = { detail: text };
  }

  if (!response.ok) {
    const detail =
      (body as { detail?: string } | null)?.detail ??
      `${response.status} ${response.statusText}`;
    throw new ApiError(detail, response.status);
  }
  return body as T;
}

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });

/* ─── Shapes, as the server actually sends them ─────────────────────────── */

export interface Status {
  version: string;
  database: string;
  candidate: string | null;
  facts: number;
  verified_facts: number;
  skills: number;
  jobs: number;
  boards: number;
  companies_researched: number;
  applications: number;
  awaiting_approval: number;
  semantic_matching: boolean;
  llm_available: boolean;
  ats_platforms: number;
  resume_archetypes: number;
  autonomy_level: number;
  autonomy_description: string;
  portal_adapters_enabled: boolean;
  checkpoints: Record<string, unknown>;
  decisions_recorded: number;
  monitoring: Record<string, unknown>;
  infrastructure: {
    rate_limits: { backend: string; keys: number };
    object_store: string;
    tracing: { enabled: boolean; backend: string; durable_record: string[] };
    multi_user: boolean;
  };
}

export interface Job {
  id: string;
  title: string;
  company: string;
  location: string;
  work_mode: string;
  employment_type: string;
  seniority: string;
  posted_date: string | null;
  ats: string | null;
  urls: string[];
  final_score: number | null;
  priority: string;
  reasons: string[];
  missing_skills: string[];
}

export interface Person {
  id: string;
  company_id: string;
  company: string;
  name: string;
  title: string;
  department: string;
  location: string;
  function: string;
  seniority: number;
  hiring_authority: boolean;
  relevance: number;
  relevance_confidence: number;
  relevance_reasons: string[];
  recommended_channel: string;
  email?: string | null;
  linkedin_url?: string | null;
}

export interface PeopleResponse {
  people: Person[];
  count: number;
  contactable: number;
  note: string;
}

export interface FunnelStage {
  stage: string;
  count: number;
  from_previous: number | null;
}

export interface Funnel {
  stages: FunnelStage[];
  discovered: number;
  scored: number;
  past_gate: number;
  strong_matches: number;
  shortlisted: number;
  prepared: number;
  submitted: number;
  responses: number;
  interviews: number;
  offers: number;
}

export interface GraphNode {
  id: string;
  kind: string;
  key: string;
  label: string;
  attrs: Record<string, unknown>;
}

export interface GraphEdge {
  id: string;
  kind: string;
  source: string;
  target: string;
  weight: number;
  attrs: Record<string, unknown>;
}

export interface GraphView {
  nodes: GraphNode[];
  edges: GraphEdge[];
  counts: Record<string, number>;
  node_count: number;
  edge_count: number;
  truncated: boolean;
  max_nodes: number;
  root?: string;
}

export interface Integration {
  id: string;
  label: string;
  env_vars: string[];
  powers: string;
  signup: string;
  optional: boolean;
  configured: boolean;
  key: string;
  costs_a_call: boolean;
}

export interface UncontactedPerson {
  person_id: string;
  name: string;
  title: string;
  company: string;
  company_id: string;
  relevance: number;
  hiring_authority: boolean;
  best_job_score: number;
  reachable: boolean;
  channel: string;
  why: string;
}

export interface UncontactedResponse {
  people: UncontactedPerson[];
  count: number;
  reachable: number;
  companies?: number;
  suppressed_skipped?: number;
  note: string;
}

export interface SkillReach {
  skill: string;
  reach: number;
  share: number;
  strength: number;
  ambiguous: boolean;
  note: string;
}

export interface UntouchedCompany {
  company: string;
  open_roles: number;
  best_score: number;
  why: string;
}

export interface ActivityEvent {
  id?: string;
  kind?: string;
  type?: string;
  message?: string;
  at?: string;
  created_at?: string;
  payload?: Record<string, unknown>;
}
