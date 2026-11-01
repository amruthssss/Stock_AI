export type WorkflowPurpose = {
  title: string;
  purpose: string;
  output: string;
  delivery: string;
};

const PURPOSE_BY_ID: Record<string, WorkflowPurpose> = {
  "G9TN4Ma0JRsefx9t": {
    title: "Daily Inventory Report · draft",
    purpose: "Creates the daily inventory report from the latest saved risk run for operator review.",
    output: "A saved report artifact with inventory risk and provenance details.",
    delivery: "Email notification is disabled in this draft.",
  },
  "Jj63wDWw8wju5Le1": {
    title: "Daily Inventory Report · published",
    purpose: "Creates the daily inventory report from the latest saved risk run.",
    output: "A saved report artifact with inventory risk and provenance details.",
    delivery: "Email notification is disabled in the workflow; its schedule is currently published.",
  },
  "59VmMJsRhCGv6299": {
    title: "Stockout Risk Alerts",
    purpose: "Reads saved stockout risk signals and prepares deduplicated operator notifications.",
    output: "Stockout alerts derived from the saved synthetic inventory-risk run.",
    delivery: "Gmail and Telegram need valid recipients and a reachable authenticated gateway.",
  },
  "FinpIKx6D9FrCFpG": {
    title: "Expiry Risk Alerts",
    purpose: "Reads saved expiry exposure signals for review before inventory decisions.",
    output: "Expiry alerts; expiry value is a proxy and the operational inventory inputs are synthetic.",
    delivery: "Email notification is disabled in the workflow.",
  },
  "fG4WvkoRkeTpu3cW": {
    title: "Overstock Alerts",
    purpose: "Reads saved overstock signals and prepares deduplicated operator notifications.",
    output: "Overstock alerts derived from the saved synthetic inventory-risk run.",
    delivery: "Gmail and Telegram need valid recipients and a reachable authenticated gateway.",
  },
  "Ci6v7edwZbknsD9n": {
    title: "Weekly Data Science Report",
    purpose: "Builds a weekly summary from saved forecast-evaluation results.",
    output: "Forecast metrics and provenance from completed evaluation runs.",
    delivery: "Email and Telegram configuration must be validated before another notification run.",
  },
  "dIUupWaMhXErWouc": {
    title: "Human Review Notifications",
    purpose: "A notification-only review queue for stockout signals; it does not approve recommendations.",
    output: "Candidate risk signals with a deduplication key and source run reference.",
    delivery: "Gateway authentication and test recipients are not fully configured.",
  },
  "09iAcHthbB1CjLtR": {
    title: "Human Approval Notifications · archived legacy flow",
    purpose: "An older combined draft containing several report and alert branches.",
    output: "Unresolved legacy graph; it includes an unconnected command node and remains archived.",
    delivery: "Do not unarchive or run; the flow is not a verified approval system.",
  },
  "6WwZY1YaptfiJcuP": {
    title: "Manual Gmail / Telegram test · archived",
    purpose: "A three-node manual notification test workflow.",
    output: "Test messages only; it is not part of the StockWise automation pipeline.",
    delivery: "Archived after failed Gmail test runs; keep archived.",
  },
};

export function workflowPurpose(workflowId: string): WorkflowPurpose {
  return PURPOSE_BY_ID[workflowId] ?? {
    title: "Unclassified n8n workflow",
    purpose: "No reviewed business purpose is recorded for this workflow ID.",
    output: "Inspect the workflow definition before using it.",
    delivery: "Not verified.",
  };
}
