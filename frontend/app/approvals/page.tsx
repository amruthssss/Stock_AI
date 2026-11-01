import { StockWiseNavigation } from "@/components/stockwise-navigation";
import { ApprovalWorkspace } from "@/app/approvals/workspace";
import {
  getInventoryRiskApprovals,
  getInventoryRiskRun,
  getInventoryRiskRuns,
} from "@/lib/stockwise-api";

export const dynamic = "force-dynamic";

type PageProps = { searchParams: Promise<{ run_id?: string }> };

export default async function ApprovalsPage({ searchParams }: PageProps) {
  const params = await searchParams;
  try {
    const runs = await getInventoryRiskRuns();
    const selectedRun = runs.items.find((run) => run.run_id === params.run_id) ?? runs.items[0] ?? null;
    const [riskDetail, history] = selectedRun
      ? await Promise.all([
          getInventoryRiskRun(selectedRun.run_id),
          getInventoryRiskApprovals(),
        ])
      : [null, { items: [], total: 0, limit: 100, offset: 0 }];

    return (
      <main className="app-main">
        <Header />
        {selectedRun && riskDetail ? (
          <>
            <section className="panel panel-hover mb-6 p-5 sm:p-6">
              <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
                <div>
                  <p className="text-xs font-bold uppercase tracking-[0.18em] text-emerald-700">Human review · no order execution</p>
                  <h2 className="mt-2 text-xl font-semibold">Review a saved replenishment suggestion</h2>
                  <p className="mt-1 max-w-2xl text-sm text-slate-600">Select a risk report, inspect a positive suggested quantity, and record an explicit decision.</p>
                </div>
                <form action="/approvals" method="get" className="flex flex-wrap items-end gap-3">
                  <label className="grid gap-1 text-xs font-semibold text-slate-600">
                    Risk report
                    <select name="run_id" defaultValue={selectedRun.run_id} className="h-10 min-w-64 rounded-lg border border-slate-300 bg-white px-3 text-sm text-slate-900">
                      {runs.items.map((run) => <option key={run.run_id} value={run.run_id}>{run.run_id} · {run.state_id}/{run.category_id}</option>)}
                    </select>
                  </label>
                  <button className="h-10 rounded-lg bg-emerald-800 px-5 text-sm font-semibold text-white hover:bg-emerald-700">Load report</button>
                </form>
              </div>
            </section>
            <ApprovalWorkspace
              riskRunId={selectedRun.run_id}
              riskItems={riskDetail.results}
              initialApprovals={history.items}
            />
            <p className="mt-5 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm leading-6 text-amber-950">
              Inventory inputs are {selectedRun.inventory_provenance}; demand reference: {selectedRun.demand_reference}. An approval records a local human review only. It does not create a purchase order, contact a supplier, or change a price.
            </p>
          </>
        ) : (
          <section className="panel p-7">
            <h2 className="text-lg font-semibold">No inventory-risk reports imported yet</h2>
            <p className="mt-2 text-sm text-slate-600">Generate and import a completed risk report before recording a review.</p>
          </section>
        )}
      </main>
    );
  } catch (error) {
    return (
      <main className="app-main">
        <Header />
        <section role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-5 text-rose-900">
          <h2 className="font-semibold">Approval data is unavailable</h2>
          <p className="mt-1 text-sm">{error instanceof Error ? error.message : "The StockWise API request failed."}</p>
          <p className="mt-2 text-sm">Start FastAPI, confirm PostgreSQL readiness, and refresh.</p>
        </section>
      </main>
    );
  }
}

function Header() {
  return (
    <header className="app-header animate-rise">
      <StockWiseNavigation active="approvals" />
      <div className="app-heading">
      <div>
        <p className="eyebrow">Human review · append-only record</p>
        <h1 className="page-title">Approvals &amp; History</h1>
        <p className="page-subtitle">Review simulated replenishment suggestions and record a human decision.</p>
      </div>
      </div>
    </header>
  );
}
