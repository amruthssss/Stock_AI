import {
  getInventoryRiskRun,
  getInventoryRiskRuns,
  type InventoryRiskItem,
} from "@/lib/stockwise-api";
import { StockWiseNavigation } from "@/components/stockwise-navigation";

export const dynamic = "force-dynamic";

type SearchParams = Promise<{ run_id?: string; sort_by?: string }>;
type PageProps = { searchParams: SearchParams };
type RiskSort = "stockout" | "expiry";

function chooseSort(value: string | undefined): RiskSort {
  return value === "expiry" ? "expiry" : "stockout";
}

function formatUnits(value: number | null, digits = 1): string {
  return value === null ? "—" : value.toFixed(digits);
}

function ErrorNotice({ message }: { message: string }) {
  return (
    <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-5 text-rose-900">
      <p className="font-semibold">Inventory-risk data is unavailable</p>
      <p className="mt-1 text-sm">{message}</p>
      <p className="mt-2 text-sm">Start FastAPI and confirm PostgreSQL readiness, then refresh this page.</p>
    </div>
  );
}

function orderResults(results: InventoryRiskItem[], sortBy: RiskSort): InventoryRiskItem[] {
  return [...results].sort((left, right) => {
    const primary = sortBy === "expiry"
      ? right.expiry_units_at_risk - left.expiry_units_at_risk
      : right.stockout_probability - left.stockout_probability;
    if (primary !== 0) return primary;
    const secondary = sortBy === "expiry"
      ? right.stockout_probability - left.stockout_probability
      : right.expiry_units_at_risk - left.expiry_units_at_risk;
    return secondary || left.series_id.localeCompare(right.series_id);
  });
}

export default async function RiskExplorerPage({ searchParams }: PageProps) {
  const params = await searchParams;
  const sortBy = chooseSort(params.sort_by);

  let runs;
  try {
    runs = await getInventoryRiskRuns();
  } catch (error) {
    return (
      <main className="app-main">
        <Header />
        <ErrorNotice message={error instanceof Error ? error.message : "The API request failed."} />
      </main>
    );
  }

  const selectedRun = runs.items.find((run) => run.run_id === params.run_id) ?? runs.items[0] ?? null;
  if (!selectedRun) {
    return (
      <main className="app-main">
        <Header />
        <section className="panel p-7">
          <h2 className="text-lg font-semibold">No inventory-risk reports imported yet</h2>
          <p className="mt-2 text-sm text-slate-600">
            Import a completed risk report into PostgreSQL, then refresh this page.
          </p>
        </section>
      </main>
    );
  }

  let detail = null;
  let dataError: string | null = null;
  try {
    detail = await getInventoryRiskRun(selectedRun.run_id);
  } catch (error) {
    dataError = error instanceof Error ? error.message : "The API request failed.";
  }

  const results = orderResults(detail?.results ?? [], sortBy);
  const stockoutCount = results.filter((item) => item.stockout_exposure_units > 0).length;
  const expiryCount = results.filter((item) => item.expiry_units_at_risk > 0).length;
  const orderCount = results.filter((item) => item.suggested_order_quantity_units > 0).length;
  const overstockCount = results.filter((item) => item.overstock_units > 0).length;
  const exposureUnits = results.reduce((total, item) => total + item.stockout_exposure_units, 0);

  return (
    <main className="app-main">
      <Header />

      {dataError ? (
        <ErrorNotice message={dataError} />
      ) : (
        <>
          <section className="panel panel-hover mb-6 p-5 sm:p-6">
            <div className="flex flex-col gap-5 xl:flex-row xl:items-end xl:justify-between">
              <div>
                <p className="text-xs font-bold uppercase tracking-[0.18em] text-emerald-700">
                  Inventory risk · simulated operational data
                </p>
                <h2 className="mt-2 text-xl font-semibold tracking-tight">Choose a risk report</h2>
                <p className="mt-1 max-w-2xl text-sm text-slate-600">
                  Inspect stockout, overstock, reorder, coverage, and expiry indicators for one saved simulation run.
                </p>
              </div>

              <form action="/risk-explorer" method="get" className="grid gap-3 lg:grid-cols-[minmax(16rem,1fr)_12rem_auto]">
                <label className="grid gap-1 text-xs font-semibold text-slate-600">
                  Risk report
                  <select
                    name="run_id"
                    defaultValue={selectedRun.run_id}
                    className="h-10 min-w-56 rounded-lg border border-slate-300 bg-white px-3 text-sm text-slate-900 outline-none focus:border-emerald-600 focus:ring-2 focus:ring-emerald-100"
                  >
                    {runs.items.map((run) => (
                      <option key={run.run_id} value={run.run_id}>
                        {run.run_id} · {run.state_id}/{run.category_id}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="grid gap-1 text-xs font-semibold text-slate-600">
                  Sort rows by
                  <select
                    name="sort_by"
                    defaultValue={sortBy}
                    className="h-10 rounded-lg border border-slate-300 bg-white px-3 text-sm text-slate-900 outline-none focus:border-emerald-600 focus:ring-2 focus:ring-emerald-100"
                  >
                    <option value="stockout">Stockout probability</option>
                    <option value="expiry">Expiry units at risk</option>
                  </select>
                </label>
                <button
                  type="submit"
                  className="h-10 self-end whitespace-nowrap rounded-lg bg-emerald-800 px-5 text-sm font-semibold text-white transition hover:bg-emerald-700 focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:ring-offset-2"
                >
                  Update view
                </button>
              </form>
            </div>
          </section>

          <section className="mb-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <SummaryCard label="Risk run" value={selectedRun.run_id} note={`As of ${selectedRun.as_of_date}`} />
            <SummaryCard label="Service level" value={`${(selectedRun.service_level * 100).toFixed(0)}%`} note={`Forecast: ${selectedRun.forecast_model}`} />
            <SummaryCard label="Series with stockout exposure" value={String(stockoutCount)} note={`${exposureUnits} exposure units at the selected service level`} />
            <SummaryCard label="Overstock signals" value={String(overstockCount)} note={`Above target of reorder point + ${selectedRun.overstock_review_period_days}-day review demand`} />
            <SummaryCard label="Expiry / order signals" value={`${expiryCount} / ${orderCount}`} note="Series with expiry exposure / suggested order" />
          </section>

          <section className="panel mb-6 overflow-hidden">
            <div className="border-b border-slate-100 px-5 py-4 sm:px-6">
              <h2 className="font-semibold">Per-series risk indicators</h2>
              <p className="mt-1 text-sm text-slate-500">
                Sorted by {sortBy === "stockout" ? "stockout probability" : "expiry units at risk"}; ties use the other risk measure.
              </p>
            </div>
            {results.length ? (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[940px] border-collapse text-left text-sm">
                  <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                    <tr>
                      <th className="px-4 py-3 font-semibold">Series</th>
                      <th className="px-4 py-3 text-right font-semibold">Stockout probability</th>
                      <th className="px-4 py-3 text-right font-semibold">Exposure units</th>
                      <th className="px-4 py-3 text-right font-semibold">Coverage days</th>
                      <th className="px-4 py-3 text-right font-semibold">Reorder point</th>
                      <th className="px-4 py-3 text-right font-semibold">Suggested order</th>
                      <th className="px-4 py-3 text-right font-semibold">Excess units</th>
                      <th className="px-4 py-3 text-right font-semibold">Overstock target</th>
                      <th className="px-4 py-3 text-right font-semibold">Expiry units</th>
                      <th className="px-4 py-3 text-right font-semibold">Expiry value proxy</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {results.map((item) => (
                      <tr key={item.series_id} className="hover:bg-slate-50/80">
                        <td className="px-4 py-3 font-medium text-slate-900">{item.series_id}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{(item.stockout_probability * 100).toFixed(1)}%</td>
                        <td className="px-4 py-3 text-right tabular-nums">{item.stockout_exposure_units}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{formatUnits(item.stock_coverage_days, 2)}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{item.reorder_point_units}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{item.suggested_order_quantity_units}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{item.overstock_units}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{item.overstock_target_units}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{formatUnits(item.expiry_units_at_risk, 2)}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{formatUnits(item.expiry_value_at_risk_proxy, 2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="p-5 text-sm text-slate-600">This risk run has no item results.</p>
            )}
          </section>

          <section className="rounded-2xl border border-amber-200 bg-amber-50 px-5 py-4 text-sm leading-6 text-amber-950">
            <p className="font-semibold">Synthetic inputs and model assumptions</p>
            <p className="mt-1">
              Operational inventory and supplier fields are {selectedRun.inventory_provenance}; the demand reference is {selectedRun.demand_reference}. Stockout probability uses a normal approximation, and expiry exposure assumes earliest-expiry-first consumption. The expiry value is a synthetic unit-cost proxy. These indicators do not establish actual retailer risk, savings, or purchase authorization.
            </p>
            <p className="mt-2 text-xs text-amber-900">Source simulation run: {selectedRun.simulation_run_id} · Selection: {selectedRun.selection_method}</p>
          </section>
        </>
      )}
    </main>
  );
}

function Header() {
  return (
    <header className="app-header animate-rise">
      <StockWiseNavigation active="risk-explorer" />
      <div className="app-heading">
      <div>
        <p className="eyebrow">Simulated operations · saved risk analysis</p>
        <h1 className="page-title">Risk Explorer</h1>
        <p className="page-subtitle">Review stockout, overstock, reorder, coverage, and expiry signals with synthetic inventory provenance.</p>
      </div>
      <span className="status-pill border-amber-200 bg-amber-50 text-amber-800"><span className="status-dot" style={{ background: "#c79a45" }} /> Synthetic inventory</span>
      </div>
    </header>
  );
}

function SummaryCard({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <article className="panel panel-hover p-5">
      <p className="metric-label">{label}</p>
      <p className="metric-value mt-3 truncate" title={value}>{value}</p>
      <p className="metric-note line-clamp-2">{note}</p>
    </article>
  );
}
