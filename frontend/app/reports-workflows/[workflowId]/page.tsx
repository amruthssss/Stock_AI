import Link from "next/link";
import { StockWiseNavigation } from "@/components/stockwise-navigation";
import { workflowPurpose } from "@/lib/n8n-workflow-catalog";
import { getN8nExecutions, getN8nWorkflow } from "@/lib/stockwise-api";

export const dynamic = "force-dynamic";

function displayDate(value: string | null): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? value
    : new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function duration(value: number | null): string {
  if (value === null) return "Not recorded";
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(1)} s`;
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

function statusStyle(status: string): string {
  if (status === "success") return "border-emerald-200 bg-emerald-50 text-emerald-800";
  if (status === "error" || status === "crashed") return "border-rose-200 bg-rose-50 text-rose-800";
  return "border-slate-200 bg-slate-50 text-slate-700";
}

export default async function WorkflowDetailsPage({
  params,
}: {
  params: Promise<{ workflowId: string }>;
}) {
  const { workflowId } = await params;
  const [workflowResult, executionResult] = await Promise.allSettled([
    getN8nWorkflow(workflowId),
    getN8nExecutions(100, workflowId),
  ]);
  const workflow = workflowResult.status === "fulfilled" ? workflowResult.value : null;
  const executions = executionResult.status === "fulfilled" ? executionResult.value.items : null;

  if (!workflow) {
    return (
      <main className="app-main">
        <header className="app-header animate-rise">
          <StockWiseNavigation active="reports-workflows" />
          <p role="status" className="mt-6 rounded-xl border border-amber-200 bg-amber-50 p-4 text-amber-950">
            The workflow details are unavailable. Confirm the backend and read-only n8n connection, then return to the Automation Center.
          </p>
          <Link href="/reports-workflows" className="mt-4 inline-flex text-sm font-semibold text-emerald-800 underline">Back to Automation Center</Link>
        </header>
      </main>
    );
  }

  const purpose = workflowPurpose(workflow.id ?? workflowId);
  const orderedExecutions = [...(executions ?? [])].sort(
    (left, right) => Date.parse(right.started_at ?? "") - Date.parse(left.started_at ?? ""),
  );
  const lastSuccess = orderedExecutions.find((execution) => execution.status === "success");
  const triggers = workflow.trigger_names?.length ? workflow.trigger_names.join(", ") : "No trigger configured";

  return (
    <main className="app-main">
      <header className="app-header animate-rise">
        <StockWiseNavigation active="reports-workflows" />
        <div className="mt-8 flex flex-wrap items-start justify-between gap-5">
          <div>
            <p className="eyebrow">Automation Center · workflow details</p>
            <h1 className="page-title">{purpose.title}</h1>
            <p className="page-subtitle">{purpose.purpose}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <span className="status-pill">{workflow.active ? "Active" : "Inactive"}</span>
            {workflow.is_archived && <span className="rounded-full border border-slate-300 bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-700">Archived</span>}
          </div>
        </div>
        <p className="mt-3 break-all font-mono text-xs text-slate-500">Workflow ID: {workflow.id ?? workflowId}</p>
      </header>

      <section className="mt-6 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <article className="panel p-5"><p className="metric-label">Schedule</p><p className="mt-2 font-semibold">{workflow.schedule ?? "Not configured"}</p><p className="mt-1 text-xs text-slate-500">Workflow timezone: {workflow.timezone ?? "n8n instance default"}</p></article>
        <article className="panel p-5"><p className="metric-label">Triggers</p><p className="mt-2 font-semibold">{triggers}</p><p className="mt-1 text-xs text-slate-500">{workflow.trigger_count} trigger node(s)</p></article>
        <article className="panel p-5"><p className="metric-label">Last execution</p><p className="mt-2 font-semibold">{orderedExecutions[0] ? displayDate(orderedExecutions[0].started_at) : "Never executed"}</p><p className="mt-1 text-xs text-slate-500">{orderedExecutions[0]?.status ?? "No execution record"}</p></article>
        <article className="panel p-5"><p className="metric-label">Last successful execution</p><p className="mt-2 font-semibold">{lastSuccess ? displayDate(lastSuccess.started_at) : "None recorded"}</p><p className="mt-1 text-xs text-slate-500">Past success does not prove current connectivity.</p></article>
      </section>

      <section className="mt-6 grid gap-4 lg:grid-cols-2">
        <article className="panel p-5">
          <h2 className="font-semibold">Purpose and output</h2>
          <p className="mt-3 text-sm leading-6 text-slate-600">{purpose.output}</p>
          <p className="mt-3 text-sm leading-6 text-slate-600">{purpose.delivery}</p>
          <p className="mt-3 text-xs leading-5 text-slate-500">The Automation Center is read-only. It never triggers a workflow or sends a message.</p>
        </article>
        <article className="panel p-5">
          <h2 className="font-semibold">Workflow steps</h2>
          {workflow.nodes?.length ? (
            <ol className="mt-3 space-y-2">
              {workflow.nodes.map((node, index) => (
                <li key={node.id ?? `${index}:${node.name}`} className="flex items-start gap-3 rounded-xl border border-slate-100 bg-white p-3">
                  <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-emerald-50 text-xs font-bold text-emerald-800">{index + 1}</span>
                  <span className="min-w-0"><span className="block text-sm font-medium text-slate-800">{node.name}{node.disabled ? " · disabled" : ""}</span><span className="mt-1 block break-all font-mono text-[11px] text-slate-500">{node.type ?? "unknown node type"}</span></span>
                </li>
              ))}
            </ol>
          ) : <p className="mt-3 text-sm text-slate-500">No safe node metadata was returned.</p>}
          <p className="mt-3 text-xs text-slate-500">Node settings, credential references, and secrets are intentionally omitted.</p>
        </article>
      </section>

      <section className="panel mt-6 overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-4 sm:px-6">
          <div><h2 className="font-semibold">Recent execution history</h2><p className="mt-1 text-sm text-slate-500">{executions === null ? "Execution history could not be loaded." : `${executions.length} recent run(s)`}</p></div>
          <Link href="/reports-workflows" className="text-sm font-semibold text-emerald-800 underline">Back to Automation Center</Link>
        </div>
        {orderedExecutions.length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-left text-sm">
              <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500"><tr><th className="px-5 py-3">Started</th><th className="px-5 py-3">Execution</th><th className="px-5 py-3">Status</th><th className="px-5 py-3">Duration</th><th className="px-5 py-3">Failure summary</th></tr></thead>
              <tbody className="divide-y divide-slate-100">
                {orderedExecutions.map((execution) => (
                  <tr key={execution.id ?? execution.started_at}>
                    <td className="px-5 py-3">{displayDate(execution.started_at)}</td>
                    <td className="px-5 py-3 font-mono text-xs">{execution.id ?? "—"}</td>
                    <td className="px-5 py-3"><span className={`rounded-full border px-2.5 py-1 text-xs font-semibold ${statusStyle(execution.status)}`}>{execution.status}</span></td>
                    <td className="px-5 py-3">{duration(executionDuration(execution))}</td>
                    <td className="max-w-lg px-5 py-3 text-xs text-rose-800">{execution.failure_summary ?? (execution.status === "error" || execution.status === "crashed" ? "Failure details are not available from the current backend." : "—")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <p className="p-5 text-sm text-slate-600">No execution records were returned.</p>}
        <p className="border-t border-slate-100 px-5 py-3 text-xs leading-5 text-slate-500">Failure summaries come from server-redacted metadata only. Inputs, outputs, workflow snapshots, and message contents are not displayed.</p>
      </section>
    </main>
  );
}
