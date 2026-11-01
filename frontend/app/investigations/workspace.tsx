"use client";

import { useEffect, useMemo, useState, type FormEvent } from "react";
import type {
  ForecastRunSummary,
  InvestigationAudit,
  InvestigationAuditList,
  InvestigationResult,
  InventoryRiskRunSummary,
} from "@/lib/stockwise-api";

type Domain = "forecast" | "inventory-risk" | "decision";
type DecisionScenario = "markdown" | "purchase_optimization" | "policy_benchmark";
type AgentStatus = "working" | "completed" | "fallback" | "failed" | "queued";

const AGENTS = [
  { id: "forecast_intelligence", label: "Forecast Intelligence", description: "Reads observed sales and saved model evaluations.", icon: "↗" },
  { id: "inventory_risk", label: "Inventory Risk", description: "Interprets modeled stockout, coverage, and expiry signals.", icon: "⌁" },
  { id: "decision_intelligence", label: "Decision Intelligence", description: "Reviews saved markdown, purchasing, and policy scenarios.", icon: "◇" },
  { id: "business_intelligence", label: "Business Intelligence", description: "Connects validated signals across domains when useful.", icon: "✳" },
] as const;

const SCENARIOS: Array<{ id: DecisionScenario; label: string }> = [
  { id: "markdown", label: "Markdown" },
  { id: "purchase_optimization", label: "Purchasing" },
  { id: "policy_benchmark", label: "Policy benchmark" },
];

function apiMessage(body: unknown, status: number): string {
  if (typeof body === "object" && body !== null) {
    if ("message" in body && typeof body.message === "string") return body.message;
    if ("detail" in body && typeof body.detail === "string") return body.detail;
  }
  return `Investigation request failed (HTTP ${status}).`;
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? value
    : new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function agentLabel(id: string): string {
  return AGENTS.find((agent) => agent.id === id)?.label ?? id.replaceAll("_", " ");
}

function prettyLabel(value: string): string {
  return value.replaceAll("_", " ").replaceAll("-", " ");
}

export function InvestigationWorkspace({
  forecastRuns,
  riskRuns,
}: {
  forecastRuns: ForecastRunSummary[];
  riskRuns: InventoryRiskRunSummary[];
}) {
  const [routingMode, setRoutingMode] = useState<"supervisor" | "manual">("supervisor");
  const [domains, setDomains] = useState<Domain[]>(["forecast"]);
  const [forecastRunId, setForecastRunId] = useState(forecastRuns[0]?.run_id ?? "");
  const [riskRunId, setRiskRunId] = useState(riskRuns[0]?.run_id ?? "");
  const [scenarios, setScenarios] = useState<DecisionScenario[]>([]);
  const [period, setPeriod] = useState<"held_out" | "rolling_origin">("held_out");
  const [question, setQuestion] = useState("");
  const [investigation, setInvestigation] = useState<InvestigationResult | null>(null);
  const [history, setHistory] = useState<InvestigationAudit[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [requestDuration, setRequestDuration] = useState<number | null>(null);
  const domainSet = useMemo(() => new Set(domains), [domains]);

  async function refreshHistory() {
    try {
      const response = await fetch("/api/investigations?limit=20", { cache: "no-store" });
      const body = await response.json();
      if (!response.ok) throw new Error(apiMessage(body, response.status));
      setHistory((body as InvestigationAuditList).items ?? []);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not load investigation history.");
    }
  }

  useEffect(() => { void refreshHistory(); }, []);

  function toggleDomain(domain: Domain) {
    setDomains((current) => current.includes(domain)
      ? current.filter((item) => item !== domain)
      : [...current, domain]);
  }

  function toggleScenario(scenario: DecisionScenario) {
    setScenarios((current) => current.includes(scenario)
      ? current.filter((item) => item !== scenario)
      : [...current, scenario]);
  }

  async function submitInvestigation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    setInvestigation(null);
    setRequestDuration(null);
    const started = performance.now();
    try {
      const payload: Record<string, unknown> = { question: question.trim(), evaluation_period: period };
      if (routingMode === "manual") {
        if (!domains.length) throw new Error("Choose at least one evidence domain, or use supervisor routing.");
        payload.domains = domains;
        const runIds: Record<string, string> = {};
        if (domainSet.has("forecast") && forecastRunId) runIds.forecast = forecastRunId;
        if (domainSet.has("inventory-risk") && riskRunId) runIds["inventory-risk"] = riskRunId;
        if (Object.keys(runIds).length) payload.run_ids = runIds;
        if (domainSet.has("decision")) payload.decision_scenarios = scenarios;
      }

      const response = await fetch("/api/investigations", {
        method: "POST",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(apiMessage(body, response.status));
      setInvestigation(body as InvestigationResult);
      setRequestDuration(Math.round(performance.now() - started));
      setMessage(body.status === "needs_clarification" ? "The supervisor needs a more specific question." : "Investigation complete. Review the source and validation details below.");
      await refreshHistory();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "The investigation could not be completed.");
    } finally {
      setBusy(false);
    }
  }

  async function openSavedInvestigation(item: InvestigationAudit) {
    setBusy(true);
    setMessage("");
    try {
      const query = new URLSearchParams({ investigation_id: item.investigation_id });
      const response = await fetch(`/api/investigations?${query.toString()}`, { cache: "no-store" });
      const body = await response.json();
      if (!response.ok) throw new Error(apiMessage(body, response.status));
      setInvestigation(body as InvestigationResult);
      if (item.evidence_type === "forecast" || item.evidence_type === "inventory-risk") {
        setRoutingMode("manual");
        setDomains([item.evidence_type]);
        if (item.evidence_type === "forecast") setForecastRunId(item.source_run_id);
        else setRiskRunId(item.source_run_id);
      }
      if (item.evaluation_period) setPeriod(item.evaluation_period);
      setMessage("Saved investigation restored from its checkpoint.");
      setRequestDuration(null);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not restore this investigation.");
    } finally {
      setBusy(false);
    }
  }

  const visibleAgents = investigation?.selected_agents ?? [];
  const resultByAgent = new Map((investigation?.agent_results ?? []).map((result) => [result.agent, result]));
  const statusByAgent = new Map(visibleAgents.map((agent) => [
    agent,
    (resultByAgent.get(agent)?.status ?? (busy ? "working" : "queued")) as AgentStatus,
  ]));

  return (
    <div className="investigation-glow -mx-2 rounded-[28px] px-2 pb-3 sm:-mx-3 sm:px-3">
      <section className="panel mb-5 overflow-hidden animate-rise p-5 sm:p-7">
        <div className="grid gap-6 xl:grid-cols-[1.05fr_.95fr] xl:items-end">
          <div>
            <div className="mb-4 flex flex-wrap items-center gap-2">
              <span className="status-pill"><span className="status-dot" /> READ ONLY · EVIDENCE FIRST</span>
              <span className="text-[10px] font-semibold text-slate-400">Local LangGraph workflow</span>
            </div>
            <h2 className="max-w-xl text-2xl font-bold tracking-[-.045em] text-[#10261d] sm:text-3xl">Ask a question.<br /><span className="text-[#729258]">Follow every answer to its source.</span></h2>
            <p className="mt-3 max-w-xl text-sm leading-6 text-slate-500">Saved numerical facts stay deterministic. Local AI can add a qualitative explanation only when its output passes evidence and safety checks.</p>
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-2 2xl:grid-cols-4">
            {AGENTS.map((agent, index) => (
              <div key={agent.id} className={`agent-card animate-rise stagger-${Math.min(index + 1, 4)} min-w-0`}>
                <span className="agent-avatar mb-2 text-base" aria-hidden="true">{agent.icon}</span>
                <p className="text-[10px] font-bold leading-4 text-[#294333]">{agent.label}</p>
                <p className="mt-1 hidden text-[9px] leading-4 text-slate-500 sm:block">{agent.description}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1.05fr)_minmax(320px,.7fr)]">
        <div>
          <section className="panel animate-rise stagger-1 p-5 sm:p-7">
            <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
              <div><p className="section-kicker">New investigation</p><h2 className="section-title mt-2">What should the team look into?</h2></div>
              <div className="rounded-xl bg-[#f0f4ed] p-1" role="group" aria-label="Routing mode">
                <button type="button" aria-pressed={routingMode === "supervisor"} onClick={() => setRoutingMode("supervisor")} className={`rounded-lg px-3 py-2 text-[10px] font-bold transition ${routingMode === "supervisor" ? "bg-white text-[#28523a] shadow-sm" : "text-slate-500 hover:text-slate-700"}`}>Auto route</button>
                <button type="button" aria-pressed={routingMode === "manual"} onClick={() => setRoutingMode("manual")} className={`rounded-lg px-3 py-2 text-[10px] font-bold transition ${routingMode === "manual" ? "bg-white text-[#28523a] shadow-sm" : "text-slate-500 hover:text-slate-700"}`}>Choose sources</button>
              </div>
            </div>

            <form onSubmit={submitInvestigation} className="grid gap-5">
              <label className="field-label">
                Your question
                <textarea required value={question} onChange={(event) => setQuestion(event.target.value)} maxLength={500} rows={3} placeholder="How does the saved demand forecast relate to current stockout risk?" className="field-control resize-y py-3 leading-6 placeholder:text-slate-400" />
                <span className="field-help flex justify-between"><span>The AI explanation is qualitative; numbers are built from validated saved evidence.</span><span>{question.length}/500</span></span>
              </label>

              {routingMode === "supervisor" ? (
                <div className="flex items-start gap-3 rounded-2xl border border-[#dce9d7] bg-[#f6faf3] p-4">
                  <span className="grid h-8 w-8 flex-none place-items-center rounded-xl bg-[#e6f0de] text-[#547b45]">✳</span>
                  <div><p className="text-xs font-bold text-[#31513a]">Supervisor selects the smallest relevant evidence set</p><p className="mt-1 text-[11px] leading-5 text-slate-500">It can read forecasts, simulated inventory risk, and saved decision scenarios. If local Ollama is unavailable, a restricted keyword router is used.</p></div>
                </div>
              ) : (
                <div className="grid gap-4 rounded-2xl border border-[#e4ebe3] bg-[#fafcf9] p-4 sm:p-5">
                  <div>
                    <p className="field-label mb-2">Evidence domains</p>
                    <div className="flex flex-wrap gap-2">
                      {(["forecast", "inventory-risk", "decision"] as const).map((domain) => (
                        <button key={domain} type="button" aria-pressed={domainSet.has(domain)} onClick={() => toggleDomain(domain)} className={`rounded-full border px-3 py-2 text-[11px] font-bold transition ${domainSet.has(domain) ? "border-[#9dbb89] bg-[#eaf3e3] text-[#31583a]" : "border-[#e0e7df] bg-white text-slate-500 hover:border-[#b8cfad]"}`}>{domain === "inventory-risk" ? "Inventory risk" : prettyLabel(domain)}</button>
                      ))}
                    </div>
                  </div>
                  {domainSet.has("forecast") && (
                    <div className="grid gap-3 sm:grid-cols-2">
                      <label className="field-label">Forecast run<select className="field-control" value={forecastRunId} onChange={(event) => setForecastRunId(event.target.value)} disabled={!forecastRuns.length}>{forecastRuns.map((run) => <option key={run.run_id} value={run.run_id}>{run.run_id} · {run.state_id}/{run.category_id}</option>)}</select></label>
                      <label className="field-label">Evaluation period<select className="field-control" value={period} onChange={(event) => setPeriod(event.target.value as "held_out" | "rolling_origin")}><option value="held_out">Held out</option><option value="rolling_origin">Rolling origin</option></select></label>
                    </div>
                  )}
                  {domainSet.has("inventory-risk") && (
                    <label className="field-label">Inventory risk run<select className="field-control" value={riskRunId} onChange={(event) => setRiskRunId(event.target.value)} disabled={!riskRuns.length}>{riskRuns.map((run) => <option key={run.run_id} value={run.run_id}>{run.run_id} · {run.state_id}/{run.category_id}</option>)}</select></label>
                  )}
                  {domainSet.has("decision") && (
                    <div><p className="field-label mb-2">Decision reports <span className="field-help">Select none to inspect all available latest reports.</span></p><div className="flex flex-wrap gap-2">{SCENARIOS.map((scenario) => <button type="button" key={scenario.id} aria-pressed={scenarios.includes(scenario.id)} onClick={() => toggleScenario(scenario.id)} className={`rounded-full border px-3 py-2 text-[10px] font-bold transition ${scenarios.includes(scenario.id) ? "border-[#9dbb89] bg-[#eaf3e3] text-[#31583a]" : "border-[#e0e7df] bg-white text-slate-500 hover:border-[#b8cfad]"}`}>{scenario.label}</button>)}</div></div>
                  )}
                  {!domains.length && <p role="alert" className="text-xs text-rose-700">Choose at least one evidence domain.</p>}
                </div>
              )}

              <div className="flex flex-wrap items-center gap-3">
                <button type="submit" disabled={busy || (routingMode === "manual" && !domains.length)} className="button-primary min-w-[190px]">
                  {busy ? <><span className="h-4 w-4 animate-spin rounded-full border-2 border-white/35 border-t-white" /> Reading evidence…</> : <>Start investigation <span aria-hidden="true">↗</span></>}
                </button>
                <span role="status" aria-live="polite" className="text-[11px] text-slate-500">{message}</span>
              </div>
            </form>
          </section>

          {busy && <div className="panel mt-5 animate-rise p-5" aria-live="polite"><p className="section-kicker">Working through approved evidence</p><div className="mt-4 space-y-3">{AGENTS.filter((agent) => routingMode === "supervisor" || domains.some((domain) => domain === "forecast" && agent.id === "forecast_intelligence" || domain === "inventory-risk" && agent.id === "inventory_risk" || domain === "decision" && agent.id === "decision_intelligence")).map((agent) => <div key={agent.id}><div className="mb-1.5 flex justify-between text-[11px]"><span className="font-semibold text-slate-700">{agent.label}</span><span className="text-slate-400">Reading saved sources</span></div><div className="agent-progress" /></div>)}</div></div>}

          {investigation && <InvestigationResultPanel result={investigation} requestDuration={requestDuration} />}
        </div>

        <aside className="space-y-5">
          {investigation && (
            <section className="panel animate-rise p-5">
              <div className="mb-4 flex items-start justify-between gap-3"><div><p className="section-kicker">Run trace</p><h2 className="section-title mt-1">Agent activity</h2></div><span className="status-pill">{investigation.ollama_call_count ?? 0} model calls</span></div>
              {(visibleAgents.length ? visibleAgents : []).map((agent) => <AgentTrace key={agent} name={agent} status={statusByAgent.get(agent) ?? "queued"} />)}
              {!visibleAgents.length && <p className="text-xs text-slate-500">No specialist was needed for this result.</p>}
              <div className="mt-4 grid grid-cols-2 gap-2 border-t border-[#e8eee8] pt-4">
                <Telemetry label="Inference" value={`${investigation.llm_inference_duration_ms ?? 0} ms`} />
                <Telemetry label="Evidence retrieval" value={`${investigation.database_retrieval_duration_ms ?? 0} ms`} />
                <Telemetry label="Request time" value={requestDuration === null ? "Restored" : `${requestDuration} ms`} />
              </div>
              <p className="mt-3 text-[10px] leading-4 text-slate-500">Inference time is reported by the local model call wrappers; request time is measured in this browser session.</p>
            </section>
          )}

          <section className="panel animate-rise stagger-2 overflow-hidden">
            <div className="border-b border-[#e8eee8] px-5 py-4"><p className="section-kicker">Saved runs · audit trail</p><h2 className="section-title mt-1">Recent investigations</h2><p className="mt-1 text-[11px] text-slate-500">Audit records contain metadata only. Restore a checkpoint to read its answer and sources.</p></div>
            {history.length ? <div className="divide-y divide-[#edf1ec]">{history.map((item) => <div key={item.investigation_id} className="flex items-center gap-3 px-5 py-3.5"><span className="grid h-8 w-8 flex-none place-items-center rounded-xl bg-[#edf3e8] text-[11px] text-[#587e4d]" aria-hidden="true">◷</span><div className="min-w-0 flex-1"><p className="truncate text-[11px] font-bold text-slate-700">{prettyLabel(item.evidence_type)}</p><p className="mt-0.5 truncate font-mono text-[9px] text-slate-400">{item.source_run_id} · {formatDate(item.created_at_utc)}</p></div><button type="button" onClick={() => void openSavedInvestigation(item)} disabled={busy} className="button-secondary min-h-[33px] px-2.5 text-[10px]">Restore</button></div>)}</div> : <p className="px-5 py-6 text-xs text-slate-500">No investigation audit records yet.</p>}
          </section>

          <section className="rounded-2xl border border-[#ece7d8] bg-[#fbf8ef] p-5">
            <div className="flex items-center gap-2"><span className="text-sm text-[#987e45]" aria-hidden="true">◎</span><h2 className="text-xs font-bold text-[#665a3e]">Provenance stays visible</h2></div>
            <p className="mt-2 text-[11px] leading-5 text-[#7e745e]">M5 sales are observed history and may be stockout-censored. Inventory, expiry, and purchasing values are synthetic assumptions. Scenario outcomes are simulations, not measured savings.</p>
          </section>
        </aside>
      </div>
    </div>
  );
}

function InvestigationResultPanel({ result, requestDuration }: { result: InvestigationResult; requestDuration: number | null }) {
  const valid = Object.values(result.validation ?? {}).every(Boolean);
  const evidence = result.evidence ?? [];
  const agents = result.agent_results ?? [];
  return (
    <section aria-label="Investigation result" className="panel mt-5 animate-rise overflow-hidden">
      <div className="flex flex-wrap items-start justify-between gap-4 border-b border-[#e8eee8] px-5 py-5 sm:px-7">
        <div><p className="section-kicker">{result.evidence_type === "forecast" ? "Observed M5 evidence" : result.evidence_type === "inventory-risk" ? "Synthetic inventory evidence" : "Multi-source saved evidence"}</p><h2 className="section-title mt-2 text-xl">Evidence-backed answer</h2><p className="mt-1 text-[11px] text-slate-500">{result.status === "completed" ? "Source facts passed the available validation checks." : `Result status: ${prettyLabel(result.status)}`}</p></div>
        <span className={`status-pill ${valid ? "" : "border-amber-200 bg-amber-50 text-amber-800"}`}><span className="status-dot" style={{ background: valid ? undefined : "#d39a42" }} />{valid ? "Validated output" : "Review validation"}</span>
      </div>
      <div className="space-y-5 px-5 py-5 sm:px-7">
        <p className="answer-block">{result.answer}</p>
        {result.llm_fallback_reason && <p className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-[11px] leading-5 text-amber-900">{result.llm_used ? "Some local AI wording was rejected by numeric-safety validation" : "Local AI fallback"}: {result.llm_fallback_reason}. StockWise kept only validated text and saved numerical evidence.</p>}
        {agents.some((agent) => agent.status === "completed") && <p className="text-[10px] leading-5 text-slate-500">Qualitative AI interpretation is cited to saved evidence but may still be incomplete. Numerical claims above are produced by deterministic application code.</p>}

        {evidence.length > 0 && <div><p className="section-kicker mb-2">Evidence sources</p><div className="flex flex-wrap gap-2">{evidence.map((item) => <span key={item.evidence_id} className="max-w-full rounded-lg border border-[#e3eae1] bg-[#f7faf5] px-2.5 py-1.5 font-mono text-[9px] text-[#506359]"><span>{prettyLabel(item.domain ?? "saved evidence")} · {item.source_run_id}</span><span className="mt-1 block break-all text-[8px] text-[#839188]">{item.evidence_id}</span></span>)}</div></div>}

        <div className="grid gap-4 lg:grid-cols-2">
          <div className="rounded-2xl border border-[#e7ede5] bg-[#fbfdf9] p-4">
            <p className="section-kicker">Validation</p>
            <div className="mt-3 space-y-2">{Object.entries(result.validation ?? {}).map(([name, passed]) => <div key={name} className="flex items-center justify-between gap-3 text-[11px]"><span className="capitalize text-slate-500">{prettyLabel(name)}</span><span className={`font-bold ${passed ? "text-[#477947]" : "text-rose-700"}`}>{passed ? "Passed" : "Failed"}</span></div>)}</div>
            <p className="section-kicker mt-5">Approved readers</p>
            <div className="mt-2 flex flex-wrap gap-1.5">{(result.tool_history ?? []).map((tool, index) => <span key={`${tool.tool_name}-${index}`} className="rounded-md bg-[#edf2eb] px-2 py-1 text-[9px] font-semibold text-[#60736a]">{prettyLabel(tool.tool_name)} · {tool.status}</span>)}</div>
          </div>
          <div className="rounded-2xl border border-[#e7ede5] bg-[#fbfdf9] p-4">
            <p className="section-kicker">Numerical facts from saved reports</p>
            {evidence.length ? <div className="mt-3 space-y-3">{evidence.map((item) => <EvidenceFacts key={item.evidence_id} item={item} />)}</div> : <p className="mt-3 text-xs text-slate-500">No evidence packet was returned.</p>}
          </div>
        </div>
        <p className="border-t border-[#e8eee8] pt-3 font-mono text-[9px] text-slate-400">Investigation {result.investigation_id}{requestDuration === null ? " · restored checkpoint" : ` · ${requestDuration} ms in browser`}</p>
      </div>
    </section>
  );
}

function EvidenceFacts({ item }: { item: InvestigationResult["evidence"][number] }) {
  const entries = Object.entries(item.facts ?? {}).slice(0, 14);
  return <details className="group rounded-xl border border-[#edf1eb] bg-white open:shadow-sm"><summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 text-[10px] font-semibold text-slate-600"><span className="truncate">{item.source_table}</span><span className="text-[#71915e] transition-transform group-open:rotate-90" aria-hidden="true">›</span></summary><div className="fact-grid px-3 pb-3">{entries.map(([key, value]) => <div key={key} className="fact-chip"><p className="fact-key">{prettyLabel(key)}</p><p className="fact-value">{typeof value === "string" ? value : typeof value === "number" || typeof value === "boolean" ? String(value) : Array.isArray(value) ? `${value.length} saved entries` : value === null ? "Not recorded" : "Structured details"}</p></div>)}</div><pre className="mx-3 mb-3 max-h-40 overflow-auto rounded-lg bg-[#10271e] p-3 text-[9px] leading-4 text-[#dcebd3]">{JSON.stringify(item.facts, null, 2)}</pre></details>;
}

function AgentTrace({ name, status }: { name: string; status: AgentStatus }) {
  const working = status === "working";
  const successful = status === "completed";
  return <div className="flex items-center gap-3 py-2.5"><span className={`grid h-8 w-8 flex-none place-items-center rounded-xl text-xs ${successful ? "bg-[#e6f1de] text-[#4a7a40]" : status === "fallback" ? "bg-amber-50 text-amber-700" : "bg-[#f0f3ef] text-slate-400"}`}>{working ? <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current/30 border-t-current" /> : successful ? "✓" : status === "fallback" ? "↺" : "·"}</span><div className="min-w-0 flex-1"><p className="truncate text-[11px] font-bold text-slate-700">{agentLabel(name)}</p><p className="text-[9px] capitalize text-slate-400">{prettyLabel(status)}</p></div>{successful && <span className="text-[9px] font-semibold text-[#679252]">Validated</span>}</div>;
}

function Telemetry({ label, value }: { label: string; value: string }) {
  return <div className="rounded-xl bg-[#f5f8f2] p-3"><p className="text-[9px] font-bold uppercase tracking-[.1em] text-slate-400">{label}</p><p className="mt-1 font-mono text-xs font-bold tabular-nums text-[#3c6144]">{value}</p></div>;
}
