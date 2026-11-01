import Link from "next/link";
import { StockWiseNavigation } from "@/components/stockwise-navigation";
import {
  getBackgroundJobs,
  getDecisionLabReports,
  getInvestigationAudits,
  getInventoryRiskApprovals,
  getN8nExecutions,
  getN8nWorkflows,
  getReports,
} from "@/lib/stockwise-api";
import { workflowPurpose } from "@/lib/n8n-workflow-catalog";

export const dynamic = "force-dynamic";

function displayDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function reportLabel(value: string): string {
  const labels: Record<string, string> = {
    series_count: "Series reviewed",
    series_with_order_recommendation: "Series with suggested orders",
    recommended_order_units: "Suggested order units",
    series_with_overstock: "Series with potential overstock",
    overstock_units: "Units above assumed target",
    expiry_units_at_risk: "Expiry units at risk",
    expiry_value_at_risk_proxy: "Expiry value proxy, not confirmed loss",
    best_held_out_model_by_mae: "Best model by held-out MAE",
    best_held_out_mae: "Held-out MAE, lower is closer",
    candidate_count: "Models compared",
    held_out_observations_per_model: "Held-out observations per model",
  };
  return labels[value] ?? value.replaceAll("_", " ");
}

function newestExecution<T extends { workflow_id: string | null; started_at: string | null }>(
  executions: T[] | null,
  workflowId: string | null,
): T | null {
  if (!executions || !workflowId) return null;
  return executions
    .filter((execution) => execution.workflow_id === workflowId)
    .sort((left, right) => Date.parse(right.started_at ?? "") - Date.parse(left.started_at ?? ""))[0] ?? null;
}

function formatDuration(value: number | null | undefined): string {
  if (value === null || value === undefined) return "Not recorded";
  if (value < 1000) return `${value} ms`;
  return `${(value / 1000).toFixed(1)} s`;
}

function executionDuration(execution: {
  duration_ms: number | null;
  started_at: string | null;
  stopped_at: string | null;
}): number | null {
  if (typeof execution.duration_ms === "number" && Number.isFinite(execution.duration_ms)) {
    return execution.duration_ms;
  }
  const started = Date.parse(execution.started_at ?? "");
  const stopped = Date.parse(execution.stopped_at ?? "");
  return Number.isFinite(started) && Number.isFinite(stopped) && stopped >= started
    ? stopped - started
    : null;
}

function executionStatusClass(status: string): string {
  if (status === "success") return "border-emerald-200 bg-emerald-50 text-emerald-800";
  if (status === "error" || status === "crashed") return "border-rose-200 bg-rose-50 text-rose-800";
  return "border-slate-200 bg-slate-50 text-slate-700";
}

function WorkflowCard({ title, status, description }: { title: string; status: string; description: string }) {
  return (
    <article className="panel panel-hover p-5">
      <div className="flex items-center justify-between gap-3">
        <h2 className="font-semibold">{title}</h2>
        <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-semibold text-slate-700">{status}</span>
      </div>
      <p className="mt-3 text-sm leading-6 text-slate-600">{description}</p>
    </article>
  );
}

export default async function ReportsWorkflowsPage() {
  const [scenarioResult, artifactResult, jobResult, investigationResult, approvalResult, n8nWorkflowResult, n8nExecutionResult] = await Promise.allSettled([
    getDecisionLabReports(),
    getReports(),
    getBackgroundJobs(20),
    getInvestigationAudits(20),
    getInventoryRiskApprovals(),
    getN8nWorkflows(100),
    getN8nExecutions(100),
  ]);

  const reports = scenarioResult.status === "fulfilled" ? scenarioResult.value.reports : null;
  const reportArtifacts = artifactResult.status === "fulfilled" ? artifactResult.value : null;
  const jobs = jobResult.status === "fulfilled" ? jobResult.value : null;
  const investigations = investigationResult.status === "fulfilled" ? investigationResult.value : null;
  const approvals = approvalResult.status === "fulfilled" ? approvalResult.value : null;
  const n8nWorkflows = n8nWorkflowResult.status === "fulfilled" ? n8nWorkflowResult.value.items : null;
  const n8nExecutions = n8nExecutionResult.status === "fulfilled" ? n8nExecutionResult.value.items : null;
  const n8nStatus = n8nWorkflows !== null && n8nExecutions !== null ? "Connected · read-only" : "Unavailable";
  const n8nError = n8nWorkflowResult.status === "rejected"
    ? n8nWorkflowResult.reason instanceof Error ? n8nWorkflowResult.reason.message : "n8n workflow metadata could not be loaded."
    : n8nExecutionResult.status === "rejected"
      ? n8nExecutionResult.reason instanceof Error ? n8nExecutionResult.reason.message : "n8n execution history could not be loaded."
      : null;
  const latestReports = reports ? Object.values(reports).filter((report) => report !== null) : [];
  const activeJobs = jobs?.items.filter((job) => job.status === "queued" || job.status === "running").length ?? 0;
  const failures = [scenarioResult, artifactResult, jobResult, investigationResult, approvalResult].filter((result) => result.status === "rejected").length;

  return (
    <main className="app-main">
      <header className="app-header animate-rise">
        <StockWiseNavigation active="reports-workflows" />
        <div className="app-heading">
        <div>
          <p className="eyebrow">Run history · local automation</p>
          <h1 className="page-title">Reports &amp; Workflows</h1>
          <p className="page-subtitle">Inspect saved scenarios, background jobs, investigations, and human reviews.</p>
        </div>
        </div>
      </header>

      {failures > 0 && (
        <p role="status" className="mb-5 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-950">
          Some local API sections could not be loaded ({failures}). Start FastAPI and confirm PostgreSQL readiness, then refresh.
        </p>
      )}

      <section className="mb-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
        <Summary label="Daily and weekly reports" value={reportArtifacts ? String(reportArtifacts.total) : "Unavailable"} note="Saved, downloadable report artifacts" />
        <Summary label="Latest saved scenarios" value={reports ? String(latestReports.length) : "Unavailable"} note="Markdown, purchasing, and policy runs" />
        <Summary label="Recent background jobs" value={jobs ? String(jobs.total) : "Unavailable"} note="Newest 20 safe status rows" />
        <Summary label="Queued or running" value={jobs ? String(activeJobs) : "Unavailable"} note="Current state in the PostgreSQL job table" />
        <Summary label="Human review records" value={approvals ? String(approvals.total) : "Unavailable"} note="Append-only local approval history" />
      </section>

      <section className="panel mb-6 overflow-hidden">
        <div className="flex items-center justify-between gap-4 border-b border-slate-100 px-5 py-4 sm:px-6">
          <div><h2 className="font-semibold">Daily and weekly reports</h2><p className="mt-1 text-sm text-slate-500">Human-readable report artifacts from saved inventory-risk and forecast runs.</p></div>
          <span className="status-pill">Read-only</span>
        </div>
        {reportArtifacts?.items.length ? (
          <div className="grid gap-4 p-5 md:grid-cols-2">
            {reportArtifacts.items.map((report) => (
              <article key={`${report.report_type}:${report.report_id}`} className="rounded-2xl border border-slate-200 bg-white p-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="section-kicker">{report.report_type === "daily_inventory" ? "Inventory operations" : "Forecast evaluation"}</p>
                    <h3 className="mt-1 font-semibold text-slate-900">{report.report_type === "daily_inventory" ? "Daily Inventory Report" : "Weekly Data Science Report"}</h3>
                  </div>
                  <span className="status-pill">{report.status}</span>
                </div>
                <p className="mt-3 text-sm text-slate-600">Generated {displayDate(report.generated_at_utc)}</p>
                <p className="mt-1 break-all font-mono text-xs text-slate-500">Source run: {report.source_run_id ?? "not recorded"}</p>
                <div className="mt-4 grid gap-2 sm:grid-cols-2">
                  {Object.entries(report.summary).slice(0, 4).map(([key, value]) => (
                    <div key={key} className="rounded-xl bg-[#f5f8f4] p-3">
                      <p className="text-[10px] font-bold uppercase tracking-wide text-slate-500">{reportLabel(key)}</p>
                      <p className="mt-1 break-words text-sm font-semibold text-slate-800">{typeof value === "object" && value !== null ? "See report details" : String(value)}</p>
                    </div>
                  ))}
                </div>
                <p className="mt-3 text-xs leading-5 text-slate-600">
                  Data provenance: {Object.entries(report.data_provenance)
                    .filter(([, value]) => ["string", "number", "boolean"].includes(typeof value))
                    .map(([key, value]) => `${reportLabel(key)}: ${String(value)}`)
                    .join(" · ") || "No provenance details were recorded."}
                </p>
                <p className="mt-4 text-xs leading-5 text-slate-600">{report.limitations}</p>
                <div className="mt-4 flex flex-wrap gap-3 text-sm font-semibold">
                  <a className="button-secondary" href={`/api/reports/${encodeURIComponent(report.report_type)}/${encodeURIComponent(report.report_id)}?format=html`}>Download HTML</a>
                  <a className="button-secondary" href={`/api/reports/${encodeURIComponent(report.report_type)}/${encodeURIComponent(report.report_id)}?format=json`}>Download JSON</a>
                </div>
              </article>
            ))}
          </div>
        ) : <p className="p-5 text-sm text-slate-600">{reportArtifacts ? "No completed daily or weekly report artifacts are available yet." : "Saved reports could not be loaded from the API."}</p>}
      </section>

      <section className="mb-6 grid gap-4 lg:grid-cols-2">
        <WorkflowCard title="PostgreSQL scenario queue" status={jobs ? "Available" : "Unavailable"} description="Decision Lab requests are queued with idempotency keys and processed by the separate local worker. Job rows below show recent processing state; this API does not expose a worker heartbeat." />
        <WorkflowCard title="Email reports" status="Dry-run supported" description="Delivery defaults to a sanitized dry-run log. Live Mailpit capture and recipient configuration have not been verified from this application session." />
      </section>

      <section className="panel mb-6 overflow-hidden" aria-labelledby="n8n-heading">
        <div className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-100 px-5 py-4 sm:px-6">
          <div><p className="section-kicker">Automation Center · read-only</p><h2 id="n8n-heading" className="mt-1 font-semibold">n8n workflows and run history</h2><p className="mt-1 text-sm text-slate-500">Live metadata from the local n8n Public API. Workflow payloads, credentials, and execution data are excluded.</p></div>
          <span className={`status-pill ${n8nWorkflows && n8nExecutions ? "" : "border-amber-200 bg-amber-50 text-amber-900"}`}>{n8nStatus}</span>
        </div>
        {n8nError ? (
          <p role="status" className="m-5 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-950">{n8nError} Add the API key to the documented local secret file and ensure the backend container has the read-only secret mount.</p>
        ) : (
          <div className="grid gap-6 p-5 xl:grid-cols-2">
            <div>
              <div className="mb-3 flex items-center justify-between"><h3 className="font-semibold">Workflow inventory</h3><span className="text-xs text-slate-500">{n8nWorkflows?.length ?? 0} returned · includes inactive and archived</span></div>
              {n8nWorkflows?.length ? <div className="space-y-3">
                {n8nWorkflows.map((workflow) => {
                  const purpose = workflowPurpose(workflow.id ?? "");
                  const lastRun = newestExecution(n8nExecutions, workflow.id);
                  const lastSuccess = newestExecution(n8nExecutions?.filter((run) => run.status === "success") ?? null, workflow.id);
                  return <article key={workflow.id ?? workflow.name} className="rounded-xl border border-slate-200 bg-white p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div><h4 className="font-semibold text-slate-900">{purpose.title}</h4><p className="mt-1 text-sm text-slate-600">{purpose.purpose}</p><p className="mt-1 break-all font-mono text-xs text-slate-500">ID {workflow.id ?? "not supplied"}</p></div>
                    <div className="flex flex-wrap gap-2"><span className="status-pill">{workflow.active ? "Active" : "Inactive"}</span>{workflow.is_archived && <span className="rounded-full border border-slate-300 bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-700">Archived</span>}</div>
                  </div>
                  <p className="mt-3 text-xs text-slate-600">{workflow.node_count} nodes · {workflow.trigger_count} triggers · {workflow.schedule ?? "No schedule details"} · timezone {workflow.timezone ?? "n8n instance default"}</p>
                  <div className="mt-3 grid gap-2 text-xs text-slate-600 sm:grid-cols-2">
                    <p>Last run: {lastRun?.started_at ? `${displayDate(lastRun.started_at)} · ${lastRun.status}` : "Never executed"}</p>
                    <p>Last successful run: {lastSuccess?.started_at ? displayDate(lastSuccess.started_at) : "No successful run recorded"}</p>
                  </div>
                  <p className="mt-2 text-xs leading-5 text-slate-600">{purpose.output} {purpose.delivery}</p>
                  {workflow.nodes?.length ? <details className="mt-3"><summary className="cursor-pointer text-sm font-medium text-emerald-900">Safe node details</summary><ul className="mt-2 space-y-1 text-xs text-slate-600">{workflow.nodes.map((node) => <li key={node.id ?? `${workflow.id}:${node.name}`} className="break-all">{node.name} <span className="font-mono text-slate-500">({node.type ?? "unknown type"})</span>{node.disabled ? " · disabled" : ""}</li>)}</ul></details> : null}
                  {workflow.id && <Link href={`/reports-workflows/${encodeURIComponent(workflow.id)}`} className="mt-4 inline-flex text-sm font-semibold text-emerald-800 underline">Open workflow details</Link>}
                </article>;
                })}
              </div> : <p className="text-sm text-slate-600">No workflows were returned.</p>}
            </div>
            <div>
              <div className="mb-3 flex items-center justify-between"><h3 className="font-semibold">Recent execution history</h3><a className="text-sm font-semibold text-emerald-800 underline" href="http://localhost:5678/home/executions" target="_blank" rel="noreferrer">Open n8n</a></div>
              {n8nExecutions?.length ? <div className="overflow-x-auto rounded-xl border border-slate-200"><table className="w-full min-w-[780px] text-left text-sm"><thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500"><tr><th className="px-4 py-3">Started</th><th className="px-4 py-3">Workflow</th><th className="px-4 py-3">Execution</th><th className="px-4 py-3">Status</th><th className="px-4 py-3">Duration / failure summary</th></tr></thead><tbody className="divide-y divide-slate-100">{n8nExecutions.map((execution) => { const workflow = n8nWorkflows?.find((item) => item.id === execution.workflow_id); const failed = execution.status === "error" || execution.status === "crashed"; return <tr key={execution.id ?? `${execution.workflow_id}:${execution.started_at}`}><td className="px-4 py-3">{execution.started_at ? displayDate(execution.started_at) : "Queued / not started"}</td><td className="px-4 py-3">{workflow?.name ?? execution.workflow_id ?? "—"}</td><td className="px-4 py-3 font-mono text-xs">{execution.id ?? "—"}</td><td className="px-4 py-3"><span className={`rounded-full border px-2.5 py-1 text-xs font-semibold ${executionStatusClass(execution.status)}`}>{execution.status}</span></td><td className="px-4 py-3 text-xs">{formatDuration(executionDuration(execution))}{failed && <p className="mt-1 max-w-md text-rose-800">{execution.failure_summary ?? "Failure details are not available from the current backend."}</p>}</td></tr>; })}</tbody></table></div> : <p className="text-sm text-slate-600">No execution history was returned.</p>}
              <p className="mt-3 text-xs leading-5 text-slate-500">Failed-node summaries use n8n redacted metadata only. Inputs, outputs, workflow snapshots, credentials, and message contents are never displayed. This page cannot start, stop, or change workflows.</p>
            </div>
          </div>
        )}
      </section>

      <section className="panel mb-6 overflow-hidden">
        <div className="flex items-center justify-between gap-4 border-b border-slate-100 px-5 py-4 sm:px-6">
          <div><h2 className="font-semibold">Latest scenario reports</h2><p className="mt-1 text-sm text-slate-500">Reports are local simulation outputs; they do not represent real orders or price changes.</p></div>
          <Link href="/decision-lab" className="text-sm font-semibold text-emerald-800 underline">Open Decision Lab</Link>
        </div>
        {reports ? (
          <div className="grid gap-4 p-5 md:grid-cols-3">
            {Object.entries(reports).map(([scenario, report]) => (
              <article key={scenario} className="rounded-xl border border-slate-200 p-4">
                <h3 className="font-semibold">{scenario.replaceAll("_", " ")}</h3>
                {report ? <><p className="mt-2 text-sm text-slate-700">Status: {report.status}</p><p className="mt-1 break-all font-mono text-xs text-slate-500">Run {report.run_id}</p></> : <p className="mt-2 text-sm text-slate-500">No saved report yet.</p>}
              </article>
            ))}
          </div>
        ) : <p className="p-5 text-sm text-slate-600">Scenario report API unavailable.</p>}
      </section>

      <section className="panel mb-6 overflow-hidden">
        <div className="border-b border-slate-100 px-5 py-4 sm:px-6"><h2 className="font-semibold">Background-job history</h2><p className="mt-1 text-sm text-slate-500">The list excludes job payloads, idempotency keys, and saved report bodies.</p></div>
        {jobs?.items.length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-left text-sm">
              <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500"><tr><th className="px-5 py-3">Created</th><th className="px-5 py-3">Type</th><th className="px-5 py-3">Job ID</th><th className="px-5 py-3">Status</th><th className="px-5 py-3">Attempt</th><th className="px-5 py-3">Error</th></tr></thead>
              <tbody className="divide-y divide-slate-100">
                {jobs.items.map((job) => <tr key={job.job_id}><td className="px-5 py-3">{displayDate(job.created_at_utc)}</td><td className="px-5 py-3">{job.job_type}</td><td className="px-5 py-3 font-mono text-xs">{job.job_id}</td><td className="px-5 py-3">{job.status}</td><td className="px-5 py-3">{job.attempt_count}/{job.max_attempts}</td><td className="px-5 py-3 text-xs text-slate-600">{job.error_message ?? "—"}</td></tr>)}
              </tbody>
            </table>
          </div>
        ) : <p className="p-5 text-sm text-slate-600">No background jobs recorded.</p>}
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <article className="panel p-5">
          <div className="flex items-center justify-between gap-3"><h2 className="font-semibold">Investigation audit history</h2><Link href="/investigations" className="text-sm font-semibold text-emerald-800 underline">Investigate</Link></div>
          <p className="mt-2 text-sm leading-6 text-slate-600">{investigations ? `${investigations.total} metadata-only audit records.` : "Audit API unavailable."} Answers and evidence are not included in these audit rows.</p>
          {investigations?.items.slice(0, 5).map((item) => <p key={item.investigation_id} className="mt-2 break-all font-mono text-xs text-slate-500">{item.investigation_id} · {item.evidence_type} · {item.status}</p>)}
        </article>
        <article className="panel p-5">
          <div className="flex items-center justify-between gap-3"><h2 className="font-semibold">Human decision history</h2><Link href="/approvals" className="text-sm font-semibold text-emerald-800 underline">Review approvals</Link></div>
          <p className="mt-2 text-sm leading-6 text-slate-600">{approvals ? `${approvals.total} append-only review records.` : "Approval history API unavailable."} A review records a decision only; it does not place an order.</p>
          {approvals?.items.slice(0, 5).map((item) => <p key={item.approval_id} className="mt-2 break-all text-xs text-slate-500">{item.decision.replaceAll("_", " ")} · {item.series_id} · {item.risk_run_id}</p>)}
        </article>
      </section>

      <p className="mt-6 rounded-xl border border-slate-200 bg-slate-50 p-4 text-xs leading-5 text-slate-600">Source distinction: forecast runs use observed public M5 sales. Inventory-risk, markdown, purchasing, and policy outputs depend on synthetic operational data and stated assumptions.</p>
    </main>
  );
}

function Summary({ label, value, note }: { label: string; value: string; note: string }) {
  return <article className="panel panel-hover p-5"><p className="metric-label">{label}</p><p className="metric-value mt-3 tabular-nums">{value}</p><p className="metric-note">{note}</p></article>;
}
