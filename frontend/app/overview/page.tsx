import Link from "next/link";
import { StockWiseNavigation } from "@/components/stockwise-navigation";
import {
  getForecastRun,
  getForecastRuns,
  getInventoryRiskRun,
  getInventoryRiskRuns,
  type ForecastMetric,
  type InventoryRiskItem,
} from "@/lib/stockwise-api";

export const dynamic = "force-dynamic";

export default async function OverviewPage() {
  const [forecastListResult, riskListResult] = await Promise.allSettled([
    getForecastRuns(),
    getInventoryRiskRuns(),
  ]);
  const forecastRuns = forecastListResult.status === "fulfilled" ? forecastListResult.value : null;
  const riskRuns = riskListResult.status === "fulfilled" ? riskListResult.value : null;
  const forecastRun = forecastRuns?.items[0] ?? null;
  const riskRun = riskRuns?.items[0] ?? null;
  const [forecastDetail, riskDetail] = await Promise.all([
    forecastRun ? getForecastRun(forecastRun.run_id).then((value) => value).catch(() => null) : Promise.resolve(null),
    riskRun ? getInventoryRiskRun(riskRun.run_id).then((value) => value).catch(() => null) : Promise.resolve(null),
  ]);

  const forecastMetrics: ForecastMetric[] = forecastDetail?.metrics
    .filter((metric) => metric.evaluation_period === "held_out") ?? [];
  const rankedModels = [...forecastMetrics].sort((a, b) => a.mae - b.mae || a.model_name.localeCompare(b.model_name));
  const bestModel = rankedModels[0] ?? null;
  const riskItems: InventoryRiskItem[] = riskDetail?.results ?? [];
  const highestRisk = [...riskItems].sort((a, b) => b.stockout_probability - a.stockout_probability || a.series_id.localeCompare(b.series_id))[0] ?? null;
  const seriesWithOrders = riskItems.filter((item) => item.suggested_order_quantity_units > 0).length;
  const expiryUnits = riskItems.reduce((total, item) => total + item.expiry_units_at_risk, 0);
  const maxMae = Math.max(...rankedModels.map((metric) => metric.mae), 0.001);
  const backendAvailable = forecastRuns !== null || riskRuns !== null;

  return (
    <main className="app-main">
      <div className="app-header animate-rise">
        <StockWiseNavigation active="overview" />
        <header className="app-heading">
          <div>
            <p className="eyebrow">Your retail intelligence workspace</p>
            <h1 className="page-title">Overview</h1>
            <p className="page-subtitle">A clear view of forecast evidence, simulated inventory signals, and the decisions that need a closer look.</p>
          </div>
          <span className="status-pill"><span className="status-dot" /> {backendAvailable ? "Local data connected" : "Waiting for local API"}</span>
        </header>
      </div>

      <section className="hero-panel mb-6 animate-rise" aria-label="StockWise introduction">
        <div className="hero-grid" />
        <div className="hero-orb animate-float" aria-hidden="true"><span className="hero-orb-core animate-pulse-soft" /></div>
        <div className="relative z-10 grid gap-8 px-6 py-8 sm:px-10 sm:py-11 lg:grid-cols-[1.3fr_.7fr] lg:items-end lg:px-12 lg:py-14">
          <div>
            <div className="mb-6 flex flex-wrap gap-2">
              <span className="data-tag">Observed M5 sales</span>
              <span className="data-tag">Synthetic stock signals</span>
              <span className="data-tag">Human-led decisions</span>
            </div>
            <h2 className="hero-title">From daily signals<br />to <em>smarter decisions.</em></h2>
            <p className="hero-copy mt-5">Explore chronological model results, identify modeled inventory pressure, and trace every recommendation back to saved evidence.</p>
            <div className="mt-7 flex flex-wrap gap-3">
              <Link href="/forecast-lab" className="button-primary">Explore forecasts <span aria-hidden="true">↗</span></Link>
              <Link href="/investigations" className="button-secondary border-white/20 bg-white/10 text-white hover:bg-white/15">Ask the evidence agents</Link>
            </div>
          </div>
          <div className="relative z-10 grid grid-cols-2 gap-3 lg:pb-1">
            <HeroStat label="Forecast scope" value={forecastRun ? `${forecastRun.series_count.toLocaleString()} series` : "No saved run"} />
            <HeroStat label="Risk scope" value={riskRun ? `${riskRun.series_count.toLocaleString()} simulated` : "No saved run"} />
            <HeroStat label="Best held-out MAE" value={bestModel ? bestModel.mae.toFixed(3) : "—"} />
            <HeroStat label="Recommended review" value={riskItems.length ? `${seriesWithOrders} series` : "—"} />
          </div>
        </div>
      </section>

      {(forecastListResult.status === "rejected" || riskListResult.status === "rejected") && (
        <div role="status" className="panel mb-6 border-amber-200 bg-amber-50/80 p-4 text-sm text-amber-950">
          Some saved data could not be loaded. Start FastAPI and check PostgreSQL readiness, then refresh this page.
        </div>
      )}

      <section className="mb-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label="Latest saved signals">
        <KpiCard index={1} label="Forecast winner" value={bestModel?.model_name ?? "Unavailable"} note={bestModel ? `${forecastRun?.state_id}/${forecastRun?.category_id} · observed M5 sales` : "No held-out evaluation available"} icon="↗" />
        <KpiCard index={2} label="Held-out MAE" value={bestModel ? bestModel.mae.toFixed(4) : "—"} note={bestModel ? `WAPE ${bestModel.wape === null ? "not recorded" : bestModel.wape.toFixed(4)} · bias ${bestModel.bias === null ? "not recorded" : bestModel.bias.toFixed(4)}` : "No forecast metric available"} icon="◌" />
        <KpiCard index={3} label="Top stockout signal" value={highestRisk ? `${(highestRisk.stockout_probability * 100).toFixed(1)}%` : "—"} note={highestRisk ? highestRisk.series_id : "No inventory-risk run available"} icon="⌁" tone="amber" />
        <KpiCard index={4} label="Expiry exposure" value={riskItems.length ? `${expiryUnits.toFixed(1)} units` : "—"} note={riskRun ? `${riskItems.length} synthetic series · ${seriesWithOrders} order signals` : "No risk result available"} icon="◷" tone="violet" />
      </section>

      <section className="mb-6 grid gap-5 xl:grid-cols-[1.15fr_.85fr]">
        <article className="panel panel-hover animate-rise p-5 sm:p-6">
          <div className="mb-5 flex flex-wrap items-start justify-between gap-4">
            <div><p className="section-kicker">Observed sales · saved run</p><h2 className="section-title mt-2">Forecast model comparison</h2><p className="mt-1 text-xs text-slate-500">Held-out MAE · lower is better</p></div>
            <Link className="button-secondary" href="/forecast-lab">Open Forecast Lab <span aria-hidden="true">↗</span></Link>
          </div>
          {rankedModels.length ? (
            <div className="space-y-4" aria-label="Held-out model MAE ranking">
              {rankedModels.slice(0, 6).map((metric, index) => (
                <div key={metric.model_name} className={`animate-rise stagger-${Math.min(index + 1, 4)}`}>
                  <div className="mb-1.5 flex items-center justify-between gap-3 text-xs">
                    <span className="flex min-w-0 items-center gap-2 font-semibold text-slate-700"><span className={`grid h-6 w-6 flex-none place-items-center rounded-lg text-[10px] ${index === 0 ? "bg-[#e7f2dd] text-[#315e37]" : "bg-slate-100 text-slate-500"}`}>{String(index + 1).padStart(2, "0")}</span><span className="truncate">{metric.model_name}</span></span>
                    <span className="flex-none font-mono font-semibold tabular-nums text-slate-800">{metric.mae.toFixed(4)}</span>
                  </div>
                  <div className="h-2 overflow-hidden rounded-full bg-[#edf1ea]"><div className={`bar-fill h-full rounded-full ${index === 0 ? "bg-gradient-to-r from-[#70a951] to-[#b6dd72]" : "bg-gradient-to-r from-[#91b9a0] to-[#b8d1bc]"}`} style={{ width: `${Math.max(6, 100 * metric.mae / maxMae)}%`, animationDelay: `${index * 70}ms` }} /></div>
                </div>
              ))}
              <p className="border-t border-[#e8eee8] pt-4 text-[11px] leading-5 text-slate-500">Forecast run {forecastRun?.run_id ?? "not available"}. One saved M5 evaluation; selected CA/FOODS series do not establish performance for all stores or future periods.</p>
            </div>
          ) : <EmptyNotice title="No forecast metrics yet" text="Import a completed forecast run to see the model comparison." />}
        </article>

        <article className="panel panel-hover animate-rise stagger-2 p-5 sm:p-6">
          <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
            <div><p className="section-kicker">Simulated operations · saved run</p><h2 className="section-title mt-2">Priority risk signals</h2><p className="mt-1 text-xs text-slate-500">Highest recorded stockout probability by series</p></div>
            <Link className="button-secondary" href={riskRun ? `/risk-explorer?run_id=${encodeURIComponent(riskRun.run_id)}` : "/risk-explorer"}>Risk Explorer <span aria-hidden="true">↗</span></Link>
          </div>
          {riskItems.length ? (
            <div className="space-y-3">
              {[...riskItems].sort((a, b) => b.stockout_probability - a.stockout_probability || a.series_id.localeCompare(b.series_id)).slice(0, 5).map((item, index) => (
                <RiskRow key={item.series_id} item={item} index={index} />
              ))}
              <div className="mt-4 rounded-xl border border-[#f0e6d2] bg-[#fbf7ef] px-4 py-3 text-[11px] leading-5 text-[#796b4f]">Risk run {riskRun?.run_id ?? "not available"}. On-hand stock, expiry, lead time, and unit costs use synthetic assumptions. Sales may be stockout-censored; these signals are not measured retailer exposure.</div>
            </div>
          ) : <EmptyNotice title="No risk report yet" text="Generate and import a simulated inventory-risk run to inspect priority signals." />}
        </article>
      </section>

      <section className="grid gap-4 md:grid-cols-3" aria-label="Continue exploring">
        <Shortcut href="/decision-lab" number="01" title="Compare decisions" text="Explore markdown, purchasing, and policy simulations." />
        <Shortcut href="/approvals" number="02" title="Review recommendations" text="Record human decisions and inspect approval history." />
        <Shortcut href="/investigations" number="03" title="Ask the agents" text="Get qualitative interpretations tied to saved evidence." />
      </section>
    </main>
  );
}

function HeroStat({ label, value }: { label: string; value: string }) {
  return <div className="rounded-2xl border border-white/10 bg-white/[.07] p-4 backdrop-blur-sm"><p className="text-[9px] font-bold uppercase tracking-[.14em] text-white/55">{label}</p><p className="mt-2 text-sm font-semibold tracking-tight text-white sm:text-base">{value}</p></div>;
}

function KpiCard({ label, value, note, icon, index, tone = "green" }: { label: string; value: string; note: string; icon: string; index: number; tone?: "green" | "amber" | "violet" }) {
  const tint = tone === "amber" ? "bg-[#fbf2df] text-[#a97a30]" : tone === "violet" ? "bg-[#f0eafa] text-[#7659a7]" : "bg-[#e9f3df] text-[#42723a]";
  return <article className={`panel panel-hover animate-rise stagger-${index} min-w-0 p-5`}><div className="flex items-start justify-between gap-3"><p className="metric-label">{label}</p><span className={`grid h-9 w-9 flex-none place-items-center rounded-xl text-lg ${tint}`} aria-hidden="true">{icon}</span></div><p className="metric-value mt-4 truncate" title={value}>{value}</p><p className="metric-note line-clamp-2">{note}</p></article>;
}

function RiskRow({ item, index }: { item: InventoryRiskItem; index: number }) {
  const percent = Math.max(0, Math.min(100, item.stockout_probability * 100));
  return <div className="animate-rise" style={{ animationDelay: `${index * 65}ms` }}><div className="mb-1.5 flex items-center justify-between gap-3"><span className="truncate text-[11px] font-semibold text-slate-700">{item.series_id}</span><span className="flex-none font-mono text-[11px] font-bold tabular-nums text-[#8d6729]">{percent.toFixed(2)}%</span></div><div className="h-1.5 overflow-hidden rounded-full bg-[#f2ede4]"><div className="bar-fill h-full rounded-full bg-gradient-to-r from-[#d6b561] to-[#e59c57]" style={{ width: `${percent}%`, animationDelay: `${index * 70}ms` }} /></div></div>;
}

function EmptyNotice({ title, text }: { title: string; text: string }) {
  return <div role="status" className="rounded-2xl border border-dashed border-[#d9e4d8] bg-[#f8faf6] px-5 py-8 text-center"><span className="mx-auto grid h-10 w-10 place-items-center rounded-xl bg-[#eaf2e5] text-lg text-[#4b7650]" aria-hidden="true">◌</span><h3 className="mt-3 text-sm font-semibold text-slate-700">{title}</h3><p className="mx-auto mt-1 max-w-xs text-xs leading-5 text-slate-500">{text}</p></div>;
}

function Shortcut({ href, number, title, text }: { href: string; number: string; title: string; text: string }) {
  return <Link href={href} className="panel panel-hover group flex items-start gap-4 p-5 no-underline"><span className="grid h-9 w-9 flex-none place-items-center rounded-xl bg-[#eef4e8] font-mono text-[10px] font-bold text-[#5a8050]">{number}</span><span><span className="block text-sm font-bold text-[#183326] group-hover:text-[#46753c]">{title} <span aria-hidden="true" className="transition-transform group-hover:translate-x-1">↗</span></span><span className="mt-1 block text-xs leading-5 text-slate-500">{text}</span></span></Link>;
}
