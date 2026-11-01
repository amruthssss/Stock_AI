export type ForecastRunSummary = {
  run_id: string;
  generated_at_utc: string;
  status: "completed";
  schema_version: number;
  source: string;
  m5_ingestion_run_id: string;
  state_id: string;
  category_id: string;
  series_count: number;
  total_matching_series: number | null;
  series_limit: number | null;
  selection_method: string;
};

export type ForecastRunList = {
  items: ForecastRunSummary[];
  total: number;
  limit: number;
  offset: number;
};

export type ForecastMetric = {
  evaluation_period: "rolling_origin" | "held_out";
  model_name: string;
  mae: number;
  wape: number | null;
  bias: number | null;
  observations: number;
};

export type ForecastRunDetail = ForecastRunSummary & {
  metrics: ForecastMetric[];
};

export type ForecastLeaderboard = {
  run_id: string;
  evaluation_period: "rolling_origin" | "held_out";
  sort_by: "mae" | "wape" | "bias";
  items: Array<ForecastMetric & { rank: number; sort_value: number | null }>;
};

export type InventoryRiskRunSummary = {
  run_id: string;
  generated_at_utc: string;
  status: "completed";
  schema_version: number;
  simulation_run_id: string;
  state_id: string;
  category_id: string;
  as_of_date: string;
  series_count: number;
  total_matching_series: number | null;
  series_limit: number | null;
  selection_method: string;
  service_level: number;
  overstock_review_period_days: number;
  forecast_model: string;
  forecast_horizon_description: string;
  inventory_provenance: string;
  demand_reference: string;
};

export type InventoryRiskList = {
  items: InventoryRiskRunSummary[];
  total: number;
  limit: number;
  offset: number;
};

export type InventoryRiskItem = {
  series_id: string;
  as_of_date: string;
  service_level: number;
  forecast_days_available: number;
  forecast_average_daily_units: number;
  stock_coverage_days: number | null;
  lead_time_days: number;
  expected_lead_time_demand_units: number;
  lead_time_demand_std_units: number;
  lead_time_demand_quantile_units: number;
  safety_stock_units: number;
  reorder_point_units: number;
  incoming_units_within_lead_time: number;
  available_supply_at_lead_time: number;
  overstock_target_units: number;
  overstock_units: number;
  stockout_probability: number;
  stockout_exposure_units: number;
  suggested_order_quantity_units: number;
  expiry_units_at_risk: number;
  expiry_value_at_risk_proxy: number;
};

export type InventoryRiskDetail = InventoryRiskRunSummary & {
  results: InventoryRiskItem[];
};

export type InventoryRiskApproval = {
  approval_id: string;
  created_at_utc: string;
  risk_run_id: string;
  series_id: string;
  decision: "approved" | "rejected" | "revision_requested";
  note: string | null;
  recommended_order_quantity_units: number;
  stockout_probability: number;
  inventory_provenance: string;
  actor: string;
};

export type InventoryRiskApprovalList = {
  items: InventoryRiskApproval[];
  total: number;
  limit: number;
  offset: number;
};

export type InvestigationEvidence = {
  domain: "forecast" | "inventory-risk" | "decision" | null;
  evidence_id: string;
  source_table: string;
  source_run_id: string;
  facts: Record<string, unknown>;
};

export type InvestigationResult = {
  investigation_id: string;
  status: "completed" | "not_found" | "validation_failed" | "needs_clarification";
  evidence_type: "forecast" | "inventory-risk" | "decision" | "business" | "multi-domain";
  answer: string;
  explanation_source: "deterministic_template" | "deterministic_template+ollama_agents" | string;
  provider: string;
  model_name: string | null;
  llm_used: boolean;
  llm_fallback_reason: string | null;
  selected_domains: string[];
  source_run_ids: Record<string, string[]>;
  selected_agents: string[];
  agent_results: Array<{
    agent: string;
    status: "completed" | "fallback" | "failed";
    summary: string;
    findings: Array<{ text: string; evidence_refs: string[] }>;
    assumptions: string[];
    missing_data: string[];
    limitations: string[];
    confidence_basis: string | null;
    evidence_refs_valid: boolean;
    numeric_claims_valid: boolean;
    call_made: boolean;
    failure_reason: string | null;
    duration_ms: number;
    output_tokens: number | null;
  }>;
  ollama_call_count: number;
  llm_inference_duration_ms: number;
  database_retrieval_duration_ms: number;
  evidence: InvestigationEvidence[];
  tool_history: Array<{ tool_name: string; status: "completed" | "not_found" | "failed" }>;
  validation: {
    schema_valid: boolean;
    numeric_claims_valid: boolean;
    evidence_references_valid: boolean;
    agent_output_valid: boolean;
  };
};

export type InvestigationAudit = {
  investigation_id: string;
  created_at_utc: string;
  evidence_type: "forecast" | "inventory-risk" | "decision" | "business" | "multi-domain";
  source_run_id: string;
  evaluation_period: "rolling_origin" | "held_out" | null;
  status: "completed" | "not_found" | "validation_failed";
  explanation_source: string;
  provider: string;
  model_name: string | null;
  llm_used: boolean;
  schema_valid: boolean;
  numeric_claims_valid: boolean;
  evidence_references_valid: boolean;
  selected_domains: string[];
  source_run_ids: Record<string, string[]>;
  selected_agents: string[];
  agent_statuses: Record<string, string>;
  ollama_call_count: number;
  llm_inference_duration_ms: number;
  agent_output_valid: boolean;
  fallback_reason: string | null;
  duration_ms: number;
};

export type InvestigationAuditList = {
  items: InvestigationAudit[];
  total: number;
  limit: number;
  offset: number;
};

export type DecisionLabReport = {
  scenario: "markdown" | "purchase_optimization" | "policy_benchmark";
  run_id: string;
  status: string;
  report: Record<string, unknown>;
};

export type DecisionLabLatest = {
  reports: Record<DecisionLabReport["scenario"], DecisionLabReport | null>;
};

export type ReportArtifactSummary = {
  report_id: string;
  report_type: "daily_inventory" | "weekly_data_science";
  status: "completed";
  generated_at_utc: string;
  source_run_id: string | null;
  summary: Record<string, unknown>;
  data_provenance: Record<string, unknown>;
  limitations: string;
  html_available: boolean;
};

export type ReportArtifactList = {
  items: ReportArtifactSummary[];
  total: number;
  limit: number;
  offset: number;
};

export type BackgroundJobSummary = {
  job_id: string;
  job_type: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  attempt_count: number;
  max_attempts: number;
  created_at_utc: string;
  updated_at_utc: string;
  error_code: string | null;
  error_message: string | null;
};

export type BackgroundJobList = {
  items: BackgroundJobSummary[];
  total: number;
  limit: number;
  offset: number;
};

export type N8nWorkflow = {
  id: string | null;
  name: string;
  active: boolean;
  is_archived: boolean;
  created_at: string | null;
  updated_at: string | null;
  timezone: string | null;
  node_count: number;
  trigger_count: number;
  schedule?: string;
  trigger_names?: string[];
  nodes?: Array<{
    id: string | null;
    name: string;
    type: string | null;
    disabled: boolean;
    is_trigger: boolean;
  }>;
};

export type N8nExecution = {
  id: string | null;
  workflow_id: string | null;
  status: string;
  mode: string | null;
  created_at: string | null;
  started_at: string | null;
  stopped_at: string | null;
  duration_ms: number | null;
  finished: boolean;
  failed_node: string | null;
  failure_summary: string | null;
};

export type N8nMetadataPage<T> = {
  items: T[];
  next_cursor: string | null;
};

const API_BASE_URL = (
  process.env.STOCKWISE_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1"
).replace(/\/+$/, "");

async function getJson<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, { cache: "no-store" });
  } catch {
    throw new Error("Could not reach the StockWise API. Check that FastAPI is running.");
  }

  if (!response.ok) {
    let message = `The StockWise API returned HTTP ${response.status}.`;
    try {
      const body: unknown = await response.json();
      if (
        typeof body === "object" &&
        body !== null &&
        "message" in body &&
        typeof body.message === "string"
      ) {
        message = body.message;
      }
    } catch {
      // Keep the safe HTTP status message if the error body is not JSON.
    }
    throw new Error(message);
  }

  return (await response.json()) as T;
}

export function getForecastRuns(): Promise<ForecastRunList> {
  return getJson<ForecastRunList>("/forecast-runs?limit=50&offset=0");
}

export function getForecastRun(runId: string): Promise<ForecastRunDetail> {
  return getJson<ForecastRunDetail>(`/forecast-runs/${encodeURIComponent(runId)}`);
}

export function getForecastLeaderboard(
  runId: string,
  period: "rolling_origin" | "held_out",
  sortBy: "mae" | "wape" | "bias",
): Promise<ForecastLeaderboard> {
  const query = new URLSearchParams({ evaluation_period: period, sort_by: sortBy });
  return getJson<ForecastLeaderboard>(
    `/forecast-runs/${encodeURIComponent(runId)}/leaderboard?${query.toString()}`,
  );
}

export function getInventoryRiskRuns(): Promise<InventoryRiskList> {
  return getJson<InventoryRiskList>("/inventory-risks?limit=50&offset=0");
}

export function getInventoryRiskRun(runId: string): Promise<InventoryRiskDetail> {
  return getJson<InventoryRiskDetail>(`/inventory-risks/${encodeURIComponent(runId)}`);
}

export function getInventoryRiskApprovals(): Promise<InventoryRiskApprovalList> {
  return getJson<InventoryRiskApprovalList>("/approvals?limit=100&offset=0");
}

export function getInvestigationAudits(limit = 20): Promise<InvestigationAuditList> {
  return getJson<InvestigationAuditList>(`/investigations?limit=${limit}&offset=0`);
}

export function getDecisionLabReports(): Promise<DecisionLabLatest> {
  return getJson<DecisionLabLatest>("/decision-lab/latest");
}

export function getReports(limit = 50): Promise<ReportArtifactList> {
  return getJson<ReportArtifactList>(`/reports?limit=${limit}&offset=0`);
}

export function getBackgroundJobs(limit = 20): Promise<BackgroundJobList> {
  return getJson<BackgroundJobList>(`/jobs?limit=${limit}&offset=0`);
}

export function getN8nWorkflows(limit = 100): Promise<N8nMetadataPage<N8nWorkflow>> {
  return getJson<N8nMetadataPage<N8nWorkflow>>(`/n8n/workflows?limit=${limit}`);
}

export function getN8nWorkflow(workflowId: string): Promise<N8nWorkflow> {
  return getJson<N8nWorkflow>(`/n8n/workflows/${encodeURIComponent(workflowId)}`);
}

export function getN8nExecutions(limit = 20, workflowId?: string): Promise<N8nMetadataPage<N8nExecution>> {
  const workflowQuery = workflowId ? `&workflowId=${encodeURIComponent(workflowId)}` : "";
  return getJson<N8nMetadataPage<N8nExecution>>(`/n8n/executions?limit=${limit}${workflowQuery}`);
}
