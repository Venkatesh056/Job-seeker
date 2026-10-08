"use client";

/**
 * Providers.
 *
 * The rule here is the reason this page is separate from settings: **a key is never
 * displayed.** The API returns a four-character fingerprint and nothing else, so
 * there is no field on this screen that could leak one and no request that could be
 * intercepted to get one. What this page shows is whether a provider *answers*, which
 * is the only thing worth knowing.
 *
 * "Configured" and "working" were once the same badge here, and that was wrong in a
 * way that cost real credits: Apollo returns `{"healthy": true}` for an account whose
 * credits are spent, so the old screen reported a dead integration as fine. Configured
 * means a key exists. Nothing on this page claims more than that.
 */

import { useEffect, useState } from "react";
import { api, type Integration } from "@/lib/api";
import { Panel, Pill } from "@/components/Panel";

export default function KeysPage() {
  const [integrations, setIntegrations] = useState<Integration[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<Integration[] | { integrations: Integration[] }>("/integrations")
      .then((data) =>
        setIntegrations(Array.isArray(data) ? data : data.integrations),
      )
      .catch((e) => setError(e.message));
  }, []);

  const ready = integrations?.filter((i) => i.configured) ?? [];
  const waiting = integrations?.filter((i) => !i.configured) ?? [];

  return (
    <div className="mx-auto max-w-[1100px] space-y-5">
      {integrations && (
        <div className="panel panel-lit rise-in flex flex-wrap items-center gap-x-8 gap-y-3 px-4 py-3">
          <div>
            <div className="text-[20px] font-semibold leading-none tabular-nums text-verdant">
              {ready.length}
            </div>
            <div className="mt-1.5 font-mono text-[9px] uppercase tracking-[0.12em] text-chalk-faint">
              configured
            </div>
          </div>
          <div>
            <div className="text-[20px] font-semibold leading-none tabular-nums text-chalk-faint">
              {waiting.length}
            </div>
            <div className="mt-1.5 font-mono text-[9px] uppercase tracking-[0.12em] text-chalk-faint">
              unset
            </div>
          </div>
          <p className="ml-auto max-w-[52ch] text-[11px] leading-relaxed text-chalk-faint">
            Keys live only in <span className="font-mono">.env</span>, which is
            gitignored. They are never logged, and this page only ever receives a
            four-character fingerprint — never a key.
          </p>
        </div>
      )}

      <Panel
        title="Connected"
        hint="A key is present. That is not the same as the provider having credit left."
        loading={integrations === null}
        error={error}
        empty={ready.length === 0 ? "Nothing configured yet." : null}
      >
        <div className="grid gap-2 sm:grid-cols-2">
          {ready.map((integration) => (
            <Card key={integration.id} integration={integration} />
          ))}
        </div>
      </Panel>

      <Panel
        title="Not set"
        hint="Each one disables exactly one capability. Nothing here stops the rest of the system."
        loading={integrations === null}
        empty={waiting.length === 0 ? "Everything is configured." : null}
        delay={80}
      >
        <div className="grid gap-2 sm:grid-cols-2">
          {waiting.map((integration) => (
            <Card key={integration.id} integration={integration} />
          ))}
        </div>
      </Panel>
    </div>
  );
}

function Card({ integration }: { integration: Integration }) {
  return (
    <div
      className={`rounded-lg border px-3 py-2.5 transition-colors ${
        integration.configured
          ? "border-ink-700 bg-ink-900/60"
          : "border-ink-800 bg-ink-900/30"
      }`}
    >
      <div className="flex items-center gap-2">
        <span
          className={`h-1.5 w-1.5 rounded-full ${integration.configured ? "bg-verdant" : "bg-ink-500"}`}
        />
        <span className="text-[13px]">{integration.label}</span>
        {integration.costs_a_call && (
          <Pill tone="warn" title="Every call spends metered quota">
            metered
          </Pill>
        )}
        {integration.configured && integration.key && (
          <span
            className="ml-auto font-mono text-[10px] text-chalk-faint"
            title="A fingerprint of the key, never the key"
          >
            {integration.key}
          </span>
        )}
      </div>

      <p className="mt-1.5 text-[11px] leading-relaxed text-chalk-dim">
        {integration.powers}
      </p>

      {!integration.configured && (
        <div className="mt-2 flex items-center gap-2">
          <code className="truncate rounded bg-ink-850 px-1.5 py-0.5 font-mono text-[10px] text-chalk-faint">
            {integration.env_vars.join(", ")}
          </code>
          <a
            href={integration.signup}
            target="_blank"
            rel="noreferrer"
            className="ml-auto shrink-0 text-[11px] text-signal hover:underline"
          >
            get a key ↗
          </a>
        </div>
      )}
    </div>
  );
}
