"use client";

import { useRef, useState, type FormEvent } from "react";
import type { InventoryRiskApproval, InventoryRiskItem } from "@/lib/stockwise-api";

type Props = {
  riskRunId: string;
  riskItems: InventoryRiskItem[];
  initialApprovals: InventoryRiskApproval[];
};

type Decision = InventoryRiskApproval["decision"];

function formatDate(value: string): string {
  return new Date(value).toLocaleString();
}

export function ApprovalWorkspace({ riskRunId, riskItems, initialApprovals }: Props) {
  const eligibleItems = riskItems.filter((item) => item.suggested_order_quantity_units > 0);
  const [selectedSeries, setSelectedSeries] = useState(eligibleItems[0]?.series_id ?? "");
  const [decision, setDecision] = useState<Decision>("approved");
  const [note, setNote] = useState("");
  const [adminToken, setAdminToken] = useState("");
  const [approvals, setApprovals] = useState(initialApprovals);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const retryKey = useRef<string | null>(null);
  const selectedItem = eligibleItems.find((item) => item.series_id === selectedSeries) ?? null;
  const runApprovals = approvals.filter((approval) => approval.risk_run_id === riskRunId);

  async function refreshHistory() {
    const response = await fetch("/api/approvals", { cache: "no-store" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.message ?? `History request failed (${response.status}).`);
    setApprovals(body.items as InventoryRiskApproval[]);
  }

  async function submitReview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedItem) return;
    setMessage("");
    setBusy(true);
    retryKey.current ??= crypto.randomUUID();
    try {
      const response = await fetch("/api/approvals", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${adminToken}`,
          "Content-Type": "application/json",
          "Idempotency-Key": retryKey.current,
        },
        body: JSON.stringify({
          risk_run_id: riskRunId,
          series_id: selectedItem.series_id,
          decision,
          note: note.trim() || null,
        }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.message ?? `Approval request failed (${response.status}).`);
      retryKey.current = null;
      setAdminToken("");
      setNote("");
      setMessage(`Review recorded: ${body.decision} for ${body.series_id}.`);
      await refreshHistory();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "The review could not be confirmed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)]">
      <section className="panel p-5 sm:p-6">
        <h2 className="text-lg font-semibold">Record a review</h2>
        <p className="mt-1 text-sm text-slate-600">The token is held only in this page's memory and cleared after a successful submission.</p>
        {eligibleItems.length ? (
          <form className="mt-5 grid gap-4" onSubmit={submitReview}>
            <label className="grid gap-1 text-sm font-medium text-slate-700">
              Recommendation
              <select value={selectedSeries} onChange={(event) => setSelectedSeries(event.target.value)} className="field-control">
                {eligibleItems.map((item) => <option key={item.series_id} value={item.series_id}>{item.series_id} · suggested {item.suggested_order_quantity_units} units</option>)}
              </select>
            </label>
            {selectedItem && (
              <div className="grid grid-cols-2 gap-3 rounded-xl border border-[#e7ede5] bg-[#f7faf5] p-4 text-sm">
                <p><span className="block text-xs text-slate-500">Suggested quantity</span><strong>{selectedItem.suggested_order_quantity_units} units</strong></p>
                <p><span className="block text-xs text-slate-500">Stockout probability</span><strong>{(selectedItem.stockout_probability * 100).toFixed(1)}%</strong></p>
              </div>
            )}
            <label className="grid gap-1 text-sm font-medium text-slate-700">
              Decision
              <select value={decision} onChange={(event) => setDecision(event.target.value as Decision)} className="field-control">
                <option value="approved">Approve for planning</option>
                <option value="rejected">Reject recommendation</option>
                <option value="revision_requested">Request revision</option>
              </select>
            </label>
            <label className="grid gap-1 text-sm font-medium text-slate-700">
              Note (optional)
              <textarea value={note} onChange={(event) => setNote(event.target.value)} maxLength={1000} rows={3} className="field-control py-3 text-sm" placeholder="Reason or follow-up for the review" />
            </label>
            <label className="grid gap-1 text-sm font-medium text-slate-700">
              Approval admin token
              <input type="password" autoComplete="off" value={adminToken} onChange={(event) => setAdminToken(event.target.value)} required className="field-control" />
            </label>
            <button type="submit" disabled={busy || !adminToken} className="button-primary disabled:cursor-not-allowed">
              {busy ? "Recording review…" : "Record review"}
            </button>
            {message && <p role="status" className="text-sm text-slate-700">{message}</p>}
          </form>
        ) : (
          <p className="mt-5 rounded-lg bg-slate-50 p-4 text-sm text-slate-600">This report has no positive suggested order quantities to review.</p>
        )}
      </section>

      <section className="panel overflow-hidden">
        <div className="border-b border-slate-100 px-5 py-4 sm:px-6">
          <h2 className="font-semibold">Decision history</h2>
          <p className="mt-1 text-sm text-slate-500">Append-only review events for this risk report.</p>
        </div>
        {runApprovals.length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[680px] text-left text-sm">
              <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500"><tr><th className="px-4 py-3">Time</th><th className="px-4 py-3">Series</th><th className="px-4 py-3">Decision</th><th className="px-4 py-3 text-right">Units</th><th className="px-4 py-3">Note</th></tr></thead>
              <tbody className="divide-y divide-slate-100">
                {runApprovals.map((approval) => (
                  <tr key={approval.approval_id}>
                    <td className="whitespace-nowrap px-4 py-3 text-slate-600">{formatDate(approval.created_at_utc)}</td>
                    <td className="px-4 py-3 font-medium">{approval.series_id}</td>
                    <td className="px-4 py-3">{approval.decision.replaceAll("_", " ")}</td>
                    <td className="px-4 py-3 text-right tabular-nums">{approval.recommended_order_quantity_units}</td>
                    <td className="max-w-52 truncate px-4 py-3 text-slate-600" title={approval.note ?? ""}>{approval.note || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <p className="p-5 text-sm text-slate-600">No review decisions recorded for this report.</p>}
      </section>
    </div>
  );
}
