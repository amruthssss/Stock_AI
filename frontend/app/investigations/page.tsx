import { StockWiseNavigation } from "@/components/stockwise-navigation";
import { InvestigationWorkspace } from "@/app/investigations/workspace";
import { getForecastRuns, getInventoryRiskRuns } from "@/lib/stockwise-api";

export const dynamic = "force-dynamic";

export default async function InvestigationsPage() {
  const [forecastResult, riskResult] = await Promise.allSettled([
    getForecastRuns(),
    getInventoryRiskRuns(),
  ]);
  const forecastRuns = forecastResult.status === "fulfilled" ? forecastResult.value.items : [];
  const riskRuns = riskResult.status === "fulfilled" ? riskResult.value.items : [];
  const unavailable = forecastResult.status === "rejected" && riskResult.status === "rejected";

  return (
    <main className="app-main">
      <header className="app-header animate-rise">
        <StockWiseNavigation active="investigations" />
        <div className="app-heading">
        <div>
          <p className="eyebrow">Governed evidence · local agent workflow</p>
          <h1 className="page-title">Agent Investigation</h1>
          <p className="page-subtitle">Ask the four specialist roles to inspect saved forecasts, simulated stock risks, or decision scenarios. Every numerical answer stays tied to validated evidence.</p>
        </div>
        </div>
      </header>

      {unavailable && (
        <p role="alert" className="mb-5 rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-900">
          The run catalog is unavailable. Start FastAPI and confirm PostgreSQL readiness, then refresh this page.
        </p>
      )}

      <InvestigationWorkspace forecastRuns={forecastRuns} riskRuns={riskRuns} />
    </main>
  );
}
