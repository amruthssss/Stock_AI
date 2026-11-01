import { DecisionLabWorkspace } from "@/app/decision-lab/workspace";
import { StockWiseNavigation } from "@/components/stockwise-navigation";

export default function DecisionLabPage() {
  return (
    <main className="app-main">
      <header className="app-header animate-rise">
        <StockWiseNavigation active="decision-lab" />
        <div className="app-heading">
        <div>
          <p className="eyebrow">Scenario analysis · no live retail changes</p>
          <h1 className="page-title">Decision Lab</h1>
          <p className="page-subtitle">Compare markdown, purchasing, and replenishment policies using simulated inventory runs.</p>
        </div>
        </div>
      </header>
      <DecisionLabWorkspace />
    </main>
  );
}
