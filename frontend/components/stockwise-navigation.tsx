import Link from "next/link";

export type StockWisePage =
  | "overview"
  | "forecast-lab"
  | "risk-explorer"
  | "decision-lab"
  | "approvals"
  | "investigations"
  | "reports-workflows";

const PAGES: Array<{ id: StockWisePage; label: string; href: string }> = [
  { id: "overview", label: "Overview", href: "/overview" },
  { id: "forecast-lab", label: "Forecast Lab", href: "/forecast-lab" },
  { id: "risk-explorer", label: "Risk Explorer", href: "/risk-explorer" },
  { id: "decision-lab", label: "Decision Lab", href: "/decision-lab" },
  { id: "approvals", label: "Approvals & History", href: "/approvals" },
  { id: "investigations", label: "Investigations", href: "/investigations" },
  { id: "reports-workflows", label: "Reports & Workflows", href: "/reports-workflows" },
];

export function StockWiseNavigation({ active }: { active: StockWisePage }) {
  return (
    <nav aria-label="StockWise pages" className="stockwise-navigation">
      <Link href="/overview" className="brand-lockup" aria-label="StockWise home">
        <span className="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none">
            <path d="M5 17.5V13m4.7 4.5V7.7m4.6 9.8v-6.2m4.7 6.2V4.5" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
            <path d="m4.5 10.5 5-3.4 4.7 2.4 5.8-5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" opacity=".7" />
          </svg>
        </span>
        <span className="brand-copy">StockWise<span className="brand-caption">Decision intelligence</span></span>
      </Link>
      <div className="nav-links">
        {PAGES.map((page) => (
          <Link
            key={page.id}
            href={page.href}
            aria-current={active === page.id ? "page" : undefined}
            className="nav-link"
          >
            {page.label}
          </Link>
        ))}
      </div>
    </nav>
  );
}
