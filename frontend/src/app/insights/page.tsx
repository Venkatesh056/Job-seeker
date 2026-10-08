"use client";

/**
 * The named questions.
 *
 * Seven traversals the backend answers by name, each chosen because a person actually
 * asks it while job hunting and the relational answer is awkward enough that nobody
 * writes it twice.
 *
 * The rule this page exists to honour: **a rate below five submissions is not shown
 * as a percentage.** One response from one application is not a 100% response rate,
 * and printing it as one makes the weakest evidence in the system look like the
 * strongest. The backend returns `null` and this prints an em dash and the reason.
 */

import { useEffect, useState } from "react";
import {
  api,
  type SkillReach,
  type UncontactedResponse,
  type UntouchedCompany,
} from "@/lib/api";
import { Measure, Percent } from "@/components/Measure";
import { Panel, Pill } from "@/components/Panel";

interface BridgingSkill {
  skill: string;
  would_connect_to: number;
  examples: string[];
  why: string;
}

interface SkillCompany {
  company: string;
  skills_matched: string[];
  skill_count: number;
  open_roles: number;
  best_score: number;
  why: string;
  openings: { job_id: string; title: string; score: number; asks_for: string[] }[];
}

interface ResumeVersion {
  resume_render_id: string;
  used: number;
  submitted: number;
  responded: number;
  interviews: number;
  rate: number | null;
  note: string;
}

export default function InsightsPage() {
  const [skillCompanies, setSkillCompanies] = useState<SkillCompany[] | null>(null);
  const [untouched, setUntouched] = useState<UntouchedCompany[] | null>(null);
  const [people, setPeople] = useState<UncontactedResponse | null>(null);
  const [bridging, setBridging] = useState<BridgingSkill[] | null>(null);
  const [reach, setReach] = useState<SkillReach[] | null>(null);
  const [worked, setWorked] = useState<{
    submitted: number;
    responded: number;
    interviews: number;
    response_rate: number | null;
    note: string;
  } | null>(null);
  const [resumes, setResumes] = useState<{
    versions: ResumeVersion[];
    note: string;
  } | null>(null);

  useEffect(() => {
    const get = <T,>(path: string, set: (value: T) => void) =>
      api<T>(path)
        .then(set)
        .catch(() => {});

    get<{ companies: SkillCompany[] }>(
      "/graph/questions/skill-companies?limit=6",
      (r) => setSkillCompanies(r.companies),
    );
    get<{ companies: UntouchedCompany[] }>(
      "/graph/questions/untouched-companies?limit=8",
      (r) => setUntouched(r.companies),
    );
    get<UncontactedResponse>(
      "/graph/questions/uncontacted-people?limit=10",
      setPeople,
    );
    get<{ skills: BridgingSkill[] }>(
      "/graph/questions/bridging-skills?limit=8",
      (r) => setBridging(r.skills),
    );
    get<{ skills: SkillReach[] }>("/graph/questions/skill-reach?limit=16", (r) =>
      setReach(r.skills),
    );
    get("/graph/questions/what-worked", setWorked);
    get("/graph/questions/resume-outcomes", setResumes);
  }, []);

  return (
    <div className="mx-auto max-w-[1280px] space-y-5">
      <div className="grid gap-5 lg:grid-cols-2">
        <Panel
          title="Where your strongest skills lead"
          hint="Ranked by how many of your best skills a company wants — not by how many roles it has open"
          loading={skillCompanies === null}
          empty={skillCompanies?.length === 0 ? "No scored openings name your skills yet." : null}
        >
          <div className="space-y-2.5">
            {skillCompanies?.map((entry) => (
              <div key={entry.company} className="rounded-lg bg-ink-900/60 px-3 py-2.5">
                <div className="flex items-center gap-2">
                  <span className="truncate text-[13px]">{entry.company}</span>
                  <Pill tone="info">{entry.skill_count} skills</Pill>
                  <div className="ml-auto flex items-center gap-2">
                    <Measure value={entry.best_score} className="w-16" height={4} />
                    <Percent
                      value={entry.best_score}
                      className="w-8 text-right font-mono text-[11px]"
                    />
                  </div>
                </div>
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {entry.skills_matched.map((skill) => (
                    <span
                      key={skill}
                      className="rounded border border-signal/25 bg-signal/[0.07] px-1.5 py-0.5 font-mono text-[10px] text-signal"
                    >
                      {skill}
                    </span>
                  ))}
                </div>
                {entry.openings[0] && (
                  <div className="mt-1.5 truncate text-[11px] text-chalk-faint">
                    e.g. {entry.openings[0].title}
                  </div>
                )}
              </div>
            ))}
          </div>
        </Panel>

        <Panel
          title="Never contacted"
          hint="Companies with open roles you score well on, that you have never written to or applied at"
          loading={untouched === null}
          empty={untouched?.length === 0 ? "You have touched everything in view." : null}
        >
          <div className="space-y-1">
            {untouched?.map((company) => (
              <div
                key={company.company}
                className="flex items-center gap-3 rounded-lg px-2 py-2 transition-colors hover:bg-ink-850"
              >
                <span className="min-w-0 flex-1 truncate text-[13px]">
                  {company.company}
                </span>
                <span className="shrink-0 font-mono text-[10px] text-chalk-faint">
                  {company.open_roles} open
                </span>
                <Measure value={company.best_score} className="w-20" height={5} />
                <Percent
                  value={company.best_score}
                  className="w-9 text-right font-mono text-[12px]"
                />
              </div>
            ))}
          </div>
        </Panel>
      </div>

      <Panel
        title="Worth writing to, and has not heard from you"
        hint="Relevant people at companies whose openings you match. Anyone who asked not to be contacted is excluded outright."
        loading={people === null}
        empty={people?.people.length === 0 ? people.note : null}
        delay={60}
        action={
          people?.suppressed_skipped ? (
            <Pill tone="warn" title="On your do-not-contact list, and therefore not shown">
              {people.suppressed_skipped} withheld
            </Pill>
          ) : null
        }
      >
        <div className="overflow-x-auto">
          <table className="w-full min-w-[620px] text-left">
            <thead>
              <tr className="font-mono text-[9px] uppercase tracking-[0.12em] text-chalk-faint">
                <th className="pb-2 font-normal">Person</th>
                <th className="pb-2 font-normal">Company</th>
                <th className="pb-2 text-right font-normal">Fit</th>
                <th className="pb-2 text-right font-normal">Best role</th>
                <th className="pb-2 pl-4 font-normal">Reach by</th>
              </tr>
            </thead>
            <tbody>
              {people?.people.map((person) => (
                <tr
                  key={person.person_id}
                  className="border-t border-ink-800 transition-colors hover:bg-ink-850"
                >
                  <td className="py-2.5 pr-3">
                    <div className="flex items-center gap-2">
                      <span className="text-[13px]">{person.name}</span>
                      {person.hiring_authority && <Pill tone="info">hires</Pill>}
                    </div>
                    <div className="mt-0.5 text-[11px] text-chalk-faint">
                      {person.title}
                    </div>
                  </td>
                  <td className="py-2.5 pr-3 text-[12px] text-chalk-dim">
                    {person.company}
                  </td>
                  <td className="py-2.5 text-right font-mono text-[12px] tabular-nums">
                    {person.relevance}
                  </td>
                  <td className="py-2.5 text-right">
                    <Percent
                      value={person.best_job_score}
                      className="font-mono text-[12px]"
                    />
                  </td>
                  <td className="py-2.5 pl-4">
                    <span
                      className={`font-mono text-[11px] ${person.reachable ? "text-chalk-dim" : "text-chalk-faint"}`}
                    >
                      {person.reachable ? person.channel || "profile" : "no channel"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {people?.note && (
          <p className="mt-3 text-[11px] leading-relaxed text-chalk-faint">
            {people.note}
          </p>
        )}
      </Panel>

      <div className="grid gap-5 lg:grid-cols-2">
        <Panel
          title="Would open the most doors"
          hint='Skills wanted by roles you already partly match. Ordinary English words — "go", "excel", "c" — are excluded.'
          loading={bridging === null}
          empty={bridging?.length === 0 ? "Not enough scored jobs yet." : null}
          delay={120}
        >
          <div className="space-y-1.5">
            {bridging?.map((skill) => {
              const top = Math.max(
                ...(bridging?.map((s) => s.would_connect_to) ?? [1]),
                1,
              );
              return (
                <div key={skill.skill} className="flex items-center gap-3 px-2 py-1">
                  <span className="w-28 shrink-0 truncate text-[13px]">
                    {skill.skill}
                  </span>
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-ink-800">
                    <div
                      className="h-full rounded-full bg-gradient-to-r from-signal-deep to-signal transition-[width] duration-700"
                      style={{ width: `${(skill.would_connect_to / top) * 100}%` }}
                    />
                  </div>
                  <span className="w-8 shrink-0 text-right font-mono text-[12px] tabular-nums">
                    {skill.would_connect_to}
                  </span>
                </div>
              );
            })}
          </div>
        </Panel>

        <Panel
          title="How far each of your skills reaches"
          hint="High reach is what to lead a resume with. Zero usually means the Fact Bank spells it differently from the market."
          loading={reach === null}
          delay={160}
        >
          <div className="space-y-1">
            {reach?.map((skill) => (
              <div
                key={skill.skill}
                className="flex items-center gap-3 px-2 py-1"
                title={skill.note || undefined}
              >
                <span
                  className={`w-28 shrink-0 truncate text-[13px] ${skill.reach === 0 ? "text-chalk-faint" : ""}`}
                >
                  {skill.skill}
                </span>
                <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-ink-800">
                  <div
                    className="h-full rounded-full transition-[width] duration-700"
                    style={{
                      width: `${Math.min(skill.share * 100 * 3, 100)}%`,
                      background: skill.ambiguous
                        ? "var(--color-amber)"
                        : "var(--color-teal)",
                    }}
                  />
                </div>
                <span className="w-10 shrink-0 text-right font-mono text-[12px] tabular-nums">
                  {skill.reach}
                </span>
                {skill.ambiguous && (
                  <span
                    className="shrink-0 text-amber"
                    title="Also an ordinary English word, so this count runs high"
                  >
                    ⚠
                  </span>
                )}
              </div>
            ))}
          </div>
        </Panel>
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Panel
          title="What actually drew a reply"
          hint="A rejection counts — it is evidence the application was read, which is exactly what silence is not."
          loading={worked === null}
          delay={200}
        >
          {worked && (
            <>
              <div className="flex gap-8">
                <Figure label="submitted" value={worked.submitted} />
                <Figure label="responded" value={worked.responded} />
                <Figure label="interviews" value={worked.interviews} />
                <div>
                  <div className="text-[20px] font-semibold leading-none tabular-nums">
                    {worked.response_rate === null ? (
                      <span className="text-chalk-faint">—</span>
                    ) : (
                      `${Math.round(worked.response_rate * 100)}%`
                    )}
                  </div>
                  <div className="mt-1.5 font-mono text-[9px] uppercase tracking-[0.12em] text-chalk-faint">
                    response rate
                  </div>
                </div>
              </div>
              <p className="mt-3 text-[11px] leading-relaxed text-chalk-faint">
                {worked.note}
              </p>
            </>
          )}
        </Panel>

        <Panel
          title="Which resume version"
          hint="A rate is withheld below five submissions rather than rounded up."
          loading={resumes === null}
          empty={
            resumes?.versions.length === 0
              ? "No application has a tailored resume attached yet."
              : null
          }
          delay={240}
        >
          <div className="space-y-1">
            {resumes?.versions.map((version) => (
              <div
                key={version.resume_render_id}
                className="flex items-center gap-3 px-2 py-1.5"
                title={version.note}
              >
                <span className="w-20 shrink-0 font-mono text-[11px] text-chalk-dim">
                  {version.resume_render_id.slice(0, 8)}
                </span>
                <span className="font-mono text-[11px] text-chalk-faint">
                  {version.submitted} sent
                </span>
                <span className="font-mono text-[11px] text-chalk-faint">
                  {version.responded} replies
                </span>
                <span className="ml-auto font-mono text-[13px] tabular-nums">
                  {version.rate === null ? (
                    <span className="text-chalk-faint">—</span>
                  ) : (
                    `${Math.round(version.rate * 100)}%`
                  )}
                </span>
              </div>
            ))}
          </div>
          {resumes?.note && (
            <p className="mt-3 text-[11px] leading-relaxed text-chalk-faint">
              {resumes.note}
            </p>
          )}
        </Panel>
      </div>
    </div>
  );
}

function Figure({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <div className="text-[20px] font-semibold leading-none tabular-nums">
        {value.toLocaleString()}
      </div>
      <div className="mt-1.5 font-mono text-[9px] uppercase tracking-[0.12em] text-chalk-faint">
        {label}
      </div>
    </div>
  );
}
