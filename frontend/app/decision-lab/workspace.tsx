"use client";

import { useEffect, useRef, useState } from "react";

type ScenarioName = "markdown" | "purchase_optimization" | "policy_benchmark";
type ScenarioResult = { scenario: ScenarioName; run_id: string; status: string; report: Record<string, unknown> };
type LatestResponse = { reports: Record<ScenarioName, ScenarioResult | null> };
type JobStatus = {
  job_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  result: ScenarioResult | null;
  error_message: string | null;
};

const SCENARIOS: Array<{ id: ScenarioName; title: string; description: string }> = [
  { id: "markdown", title: "Markdown scenarios", description: "Compare the declared discount-response scenarios without changing prices." },
  { id: "purchase_optimization", title: "Budget-constrained purchasing", description: "Use OR-Tools to allocate the budget across simulated pack sizes and minimum order quantities." },
  { id: "policy_benchmark", title: "Replenishment policy benchmark", description: "Compare policies using the latest matching inventory, risk, and optimization runs." },
];

export function DecisionLabWorkspace() {
  const [reports, setReports] = useState<Partial<Record<ScenarioName, ScenarioResult | null>>>({});
  const [activeScenario, setActiveScenario] = useState<ScenarioName>("markdown");
  const [budget, setBudget] = useState("100");
  const [adminToken, setAdminToken] = useState("");
  const [busy, setBusy] = useState<ScenarioName | null>(null);
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const idempotencyKeys = useRef<Partial<Record<ScenarioName, string>>>({});

  useEffect(() => {
    let current = true;
    fetch("/api/decision-lab", { cache: "no-store" })
      .then(async (response) => {
        const body = await response.json();
        if (!response.ok) throw new Error(body.message ?? `Could not load reports (${response.status}).`);
        if (current) setReports((body as LatestResponse).reports);
      })
      .catch((error: unknown) => {
        if (current) setMessage(error instanceof Error ? error.message : "Could not load saved reports.");
      })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, []);

  async function runScenario(scenario: ScenarioName) {
    setBusy(scenario);
    setMessage("");
    try {
      const payload: { scenario: ScenarioName; budget?: number; max_seconds: number } = {
        scenario,
        max_seconds: 5,
      };
      if (scenario === "purchase_optimization") payload.budget = Number(budget);
      idempotencyKeys.current[scenario] ??= crypto.randomUUID();
      const response = await fetch("/api/decision-lab", {
        method: "POST",
        cache: "no-store",
        headers: {
          Authorization: `Bearer ${adminToken}`,
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKeys.current[scenario],
        },
        body: JSON.stringify(payload),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.message ?? `Scenario failed (${response.status}).`);
      const acceptedJob = body as JobStatus;
      setAdminToken("");
      setMessage(`Scenario queued as ${acceptedJob.job_id}. Waiting for the worker…`);
      const completedJob = await waitForJob(acceptedJob.job_id);
      if (completedJob.status !== "succeeded" || !completedJob.result) {
        throw new Error(completedJob.error_message ?? `Scenario job ended with status ${completedJob.status}.`);
      }
      const result = completedJob.result;
      setReports((previous) => ({ ...previous, [scenario]: result }));
      setActiveScenario(scenario);
      idempotencyKeys.current[scenario] = undefined;
      setMessage(`${SCENARIOS.find((item) => item.id === scenario)?.title} completed (${result.status}).`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "The scenario could not be completed.");
    } finally {
      setBusy(null);
    }
  }

  async function waitForJob(jobId: string): Promise<JobStatus> {
    for (let attempt = 0; attempt < 180; attempt += 1) {
      const response = await fetch(`/api/decision-lab?job_id=${encodeURIComponent(jobId)}`, { cache: "no-store" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.message ?? `Job status request failed (${response.status}).`);
      const job = body as JobStatus;
      if (job.status === "succeeded" || job.status === "failed" || job.status === "cancelled") return job;
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    throw new Error("The scenario is still running. Its job ID is retained by the API; refresh later to check status.");
  }

  const activeReport = reports[activeScenario];

  return (
    <>
      <section className="mb-6 rounded-2xl border border-amber-200 bg-amber-50 p-5 text-sm leading-6 text-amber-950">
        <p className="font-semibold">Simulated scenarios only</p>
        <p className="mt-1">The calculations use synthetic stock, supplier constraints, and cost assumptions with observed M5 sales as a potentially stockout-censored demand reference. No price is changed and no purchase order is placed.</p>
      </section>

      <section className="mb-6 grid gap-4 lg:grid-cols-3">
        {SCENARIOS.map((scenario) => {
          const report = reports[scenario.id];
          return (
            <article key={scenario.id} className="panel panel-hover p-5">
              <h2 className="font-semibold">{scenario.title}</h2>
              <p className="mt-2 min-h-12 text-sm leading-5 text-slate-600">{scenario.description}</p>
              {scenario.id === "purchase_optimization" && (
                <label className="mt-3 grid gap-1 text-xs font-semibold text-slate-600">
                  Scenario budget
                  <input type="number" min="0" max="1000000" step="0.01" value={budget} onChange={(event) => setBudget(event.target.value)} className="field-control font-normal" />
                </label>
              )}
              <div className="mt-4 flex items-center justify-between gap-2">
                <span className="text-xs text-slate-500">{report ? `Latest: ${report.status}` : "No saved run"}</span>
                <button type="button" onClick={() => { setActiveScenario(scenario.id); if (report) return; }} className="text-xs font-semibold text-emerald-800 underline">View result</button>
              </div>
              <button type="button" disabled={Boolean(busy) || !adminToken || (scenario.id === "purchase_optimization" && !Number.isFinite(Number(budget)))} onClick={() => void runScenario(scenario.id)} className="button-primary mt-4 w-full disabled:cursor-not-allowed">
                {busy === scenario.id ? "Running…" : `Run ${scenario.title}`}
              </button>
            </article>
          );
        })}
      </section>

      <section className="panel mb-6 p-5 sm:p-6">
        <label className="grid max-w-xl gap-1 text-sm font-medium text-slate-700">
          Admin token (required to run a scenario)
          <input type="password" autoComplete="off" value={adminToken} onChange={(event) => setAdminToken(event.target.value)} className="field-control" />
        </label>
        <p className="mt-2 text-xs text-slate-500">The token remains in page memory, is sent only to the same-origin proxy and local API, and is cleared after a successful run. Configure it as `STOCKWISE_APPROVAL_ADMIN_TOKEN` in the ignored `.env` file.</p>
        {message && <p role="status" className="mt-4 text-sm text-slate-700">{message}</p>}
      </section>

      <section className="panel overflow-hidden">
        <div className="border-b border-slate-100 px-5 py-4 sm:px-6">
          <h2 className="font-semibold">Scenario result</h2>
          <p className="mt-1 text-sm text-slate-500">{loading ? "Loading saved reports…" : activeReport ? `${activeScenario} · ${activeReport.run_id} · ${activeReport.status}` : "No result for this scenario yet."}</p>
        </div>
        {message && !activeReport ? <p className="p-5 text-sm text-rose-800" role="alert">{message}</p> : activeReport ? (
          <details className="p-5">
            <summary className="cursor-pointer text-sm font-semibold text-emerald-900">Inspect full saved report</summary>
            <pre className="mt-4 max-h-[36rem] overflow-auto rounded-lg bg-slate-950 p-4 text-xs leading-5 text-emerald-50">{JSON.stringify(activeReport.report, null, 2)}</pre>
          </details>
        ) : <p className="p-5 text-sm text-slate-600">Run a scenario above or generate one with its documented Python runner, then refresh this page.</p>}
      </section>
    </>
  );
}
