import {
  getForecastLeaderboard,
  getForecastRun,
  getForecastRuns,
  type ForecastLeaderboard,
  type ForecastMetric,
  type ForecastRunDetail,
} from "@/lib/stockwise-api";
import Link from "next/link";
import { StockWiseNavigation } from "@/components/stockwise-navigation";

export const dynamic = "force-dynamic";

type SearchParams = Promise<{
  run_id?: string;
  period?: string;
  sort_by?: string;
}>;

type PageProps = { searchParams: SearchParams };

function choosePeriod(value: string | undefined): "rolling_origin" | "held_out" {
  return value === "rolling_origin" ? "rolling_origin" : "held_out";
}

function chooseSort(value: string | undefined): "mae" | "wape" | "bias" {
  if (value === "wape" || value === "bias") return value;
  return "mae";
}

function formatMetric(value: number | null): string {
  return value === null ? "—" : value.toFixed(3);
}

function formatUtcDate(value: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(new Date(value)) + " UTC";
}

function ErrorNotice({ message }: { message: string }) {
  return (
    <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-5 text-rose-900">
      <p className="font-semibold">Forecast data is unavailable</p>
      <p className="mt-1 text-sm">{message}</p>
      <p className="mt-2 text-sm">Start FastAPI and confirm PostgreSQL readiness, then refresh this page.</p>
    </div>
  );
}

function MetricTable({ metrics }: { metrics: ForecastMetric[] }) {
  if (metrics.length === 0) {
    return <p className="p-5 text-sm text-slate-600">No metrics were saved for this run.</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[650px] border-collapse text-left text-sm">
        <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="px-5 py-3 font-semibold">Evaluation window</th>
            <th className="px-5 py-3 font-semibold">Model</th>
            <th className="px-5 py-3 text-right font-semibold">MAE</th>
            <th className="px-5 py-3 text-right font-semibold">WAPE</th>
            <th className="px-5 py-3 text-right font-semibold">Bias</th>
            <th className="px-5 py-3 text-right font-semibold">Observations</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {metrics.map((metric) => (
            <tr key={`${metric.evaluation_period}-${metric.model_name}`} className="hover:bg-slate-50/80">
              <td className="px-5 py-3 capitalize text-slate-600">
                {metric.evaluation_period.replaceAll("_", " ")}
              </td>
              <td className="px-5 py-3 font-medium text-slate-900">{metric.model_name}</td>
              <td className="px-5 py-3 text-right tabular-nums">{formatMetric(metric.mae)}</td>
              <td className="px-5 py-3 text-right tabular-nums">{formatMetric(metric.wape)}</td>
              <td className="px-5 py-3 text-right tabular-nums">{formatMetric(metric.bias)}</td>
              <td className="px-5 py-3 text-right tabular-nums text-slate-600">
                {metric.observations.toLocaleString("en-US")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default async function ForecastLabPage({ searchParams }: PageProps) {
  const params = await searchParams;
  const period = choosePeriod(params.period);
  const sortBy = chooseSort(params.sort_by);

  let runs;
  try {
    runs = await getForecastRuns();
  } catch (error) {
    return (
      <main className="app-main">
        <Header />
        <ErrorNotice message={error instanceof Error ? error.message : "The API request failed."} />
      </main>
    );
  }

  const selectedRun =
    runs.items.find((run) => run.run_id === params.run_id) ?? runs.items[0] ?? null;

  if (!selectedRun) {
    return (
      <main className="app-main">
        <Header />
        <section className="panel p-7">
          <h2 className="text-lg font-semibold">No forecast runs imported yet</h2>
          <p className="mt-2 text-sm text-slate-600">
            Import a completed forecast JSON run into PostgreSQL, then refresh this page.
          </p>
        </section>
      </main>
    );
  }

  let detail: ForecastRunDetail | null = null;
  let leaderboard: ForecastLeaderboard | null = null;
  let dataError: string | null = null;
  try {
    [detail, leaderboard] = await Promise.all([
      getForecastRun(selectedRun.run_id),
      getForecastLeaderboard(selectedRun.run_id, period, sortBy),
    ]);
  } catch (error) {
    dataError = error instanceof Error ? error.message : "The API request failed.";
  }

  return (
    <main className="app-main">
      <Header />

      <section className="panel panel-hover mb-6 p-5 sm:p-6">
        <div className="flex flex-col gap-5 xl:flex-row xl:items-end xl:justify-between">
          <div>
            <p className="text-xs font-bold uppercase tracking-[0.18em] text-emerald-700">
              Forecast evaluation
            </p>
            <h2 className="mt-2 text-xl font-semibold tracking-tight">Choose a saved run</h2>
            <p className="mt-1 max-w-2xl text-sm text-slate-600">
              Compare models on the same run and evaluation window. These metrics describe observed M5 sales.
            </p>
          </div>

          <form action="/forecast-lab" method="get" className="grid gap-3 lg:grid-cols-4">
            <label className="grid gap-1 text-xs font-semibold text-slate-600">
              Forecast run
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
              Evaluation window
              <select
                name="period"
                defaultValue={period}
                className="h-10 rounded-lg border border-slate-300 bg-white px-3 text-sm text-slate-900 outline-none focus:border-emerald-600 focus:ring-2 focus:ring-emerald-100"
              >
                <option value="held_out">Held out</option>
                <option value="rolling_origin">Rolling origin</option>
              </select>
            </label>
            <label className="grid gap-1 text-xs font-semibold text-slate-600">
              Rank by
              <select
                name="sort_by"
                defaultValue={sortBy}
                className="h-10 rounded-lg border border-slate-300 bg-white px-3 text-sm text-slate-900 outline-none focus:border-emerald-600 focus:ring-2 focus:ring-emerald-100"
              >
                <option value="mae">MAE</option>
                <option value="wape">WAPE</option>
                <option value="bias">Absolute bias</option>
              </select>
            </label>
            <button
              type="submit"
              className="h-10 self-end rounded-lg bg-emerald-800 px-5 text-sm font-semibold text-white transition hover:bg-emerald-700 focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:ring-offset-2"
            >
              Update view
            </button>
          </form>
        </div>
      </section>

      {dataError ? (
        <ErrorNotice message={dataError} />
      ) : (
        <>
          <section className="mb-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <SummaryCard label="Run status" value={detail?.status ?? selectedRun.status} note={formatUtcDate(selectedRun.generated_at_utc)} />
            <SummaryCard
              label="M5 subset"
              value={`${selectedRun.state_id} · ${selectedRun.category_id}`}
              note={`${selectedRun.series_count.toLocaleString("en-US")} selected of ${selectedRun.total_matching_series?.toLocaleString("en-US") ?? "unknown"}`}
            />
            <SummaryCard label="Metric window" value={period === "held_out" ? "Held-out" : "Rolling origin"} note="Chronological evaluation" />
            <SummaryCard label="Models compared" value={String(leaderboard?.items.length ?? 0)} note={`Ordered by ${sortBy === "bias" ? "absolute bias" : sortBy.toUpperCase()}`} />
          </section>

          <section className="panel mb-6 overflow-hidden">
            <div className="flex flex-col gap-1 border-b border-slate-100 px-5 py-4 sm:px-6">
              <h2 className="font-semibold">Model leaderboard</h2>
              <p className="text-sm text-slate-500">
                Rank 1 is lowest for MAE and WAPE; for bias it is closest to zero.
              </p>
            </div>
            {leaderboard?.items.length ? (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[600px] border-collapse text-left text-sm">
                  <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                    <tr>
                      <th className="px-5 py-3 font-semibold">Rank</th>
                      <th className="px-5 py-3 font-semibold">Model</th>
                      <th className="px-5 py-3 text-right font-semibold">MAE</th>
                      <th className="px-5 py-3 text-right font-semibold">WAPE</th>
                      <th className="px-5 py-3 text-right font-semibold">Bias</th>
                      <th className="px-5 py-3 text-right font-semibold">Observations</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {leaderboard.items.map((item) => (
                      <tr key={item.model_name} className={item.rank === 1 ? "bg-emerald-50/60" : "hover:bg-slate-50/80"}>
                        <td className="px-5 py-3 font-semibold text-emerald-800">{item.rank}</td>
                        <td className="px-5 py-3 font-medium text-slate-900">{item.model_name}</td>
                        <td className="px-5 py-3 text-right tabular-nums">{formatMetric(item.mae)}</td>
                        <td className="px-5 py-3 text-right tabular-nums">{formatMetric(item.wape)}</td>
                        <td className="px-5 py-3 text-right tabular-nums">{formatMetric(item.bias)}</td>
                        <td className="px-5 py-3 text-right tabular-nums text-slate-600">{item.observations.toLocaleString("en-US")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="p-5 text-sm text-slate-600">No metrics are available for the selected window.</p>
            )}
          </section>

          <section className="panel mb-6 overflow-hidden">
            <div className="border-b border-slate-100 px-5 py-4 sm:px-6">
              <h2 className="font-semibold">All saved metrics for this run</h2>
              <p className="mt-1 text-sm text-slate-500">Both rolling-origin and held-out aggregates.</p>
            </div>
            <MetricTable metrics={detail?.metrics ?? []} />
          </section>

          <section className="rounded-2xl border border-amber-200 bg-amber-50 px-5 py-4 text-sm leading-6 text-amber-950">
            <p className="font-semibold">Interpretation limits</p>
            <p className="mt-1">
              {selectedRun.source}. The chosen series may be a deterministic subset. M5 records observed sales, which can be lower than latent demand during stockouts. These backtest results do not prove future accuracy, retailer savings, or causal impact.
            </p>
            <p className="mt-2 text-xs text-amber-900">M5 ingestion run: {selectedRun.m5_ingestion_run_id}</p>
          </section>
        </>
      )}
    </main>
  );
}

function Header() {
  return (
    <header className="app-header animate-rise">
      <StockWiseNavigation active="forecast-lab" />
      <div className="app-heading">
      <div>
        <p className="eyebrow">Model evaluation · observed M5 sales</p>
        <h1 className="page-title">Forecast Lab</h1>
        <p className="page-subtitle">Inspect chronological evaluation windows, model comparisons, and persisted run provenance.</p>
      </div>
      <span className="status-pill"><span className="status-dot" /> Read only · local API</span>
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
