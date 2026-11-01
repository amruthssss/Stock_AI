import { NextRequest, NextResponse } from "next/server";

const API_BASE_URL = (
  process.env.STOCKWISE_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1"
).replace(/\/+$/, "");

function safeError(status: number, message: string) {
  return NextResponse.json({ message }, { status, headers: { "Cache-Control": "no-store" } });
}

async function relay(response: Response) {
  return new NextResponse(await response.text(), {
    status: response.status,
    headers: {
      "Content-Type": response.headers.get("content-type") ?? "application/json",
      "Cache-Control": "no-store",
    },
  });
}

export async function GET(request: NextRequest) {
  const investigationId = request.nextUrl.searchParams.get("investigation_id");
  const query = investigationId
    ? `/${encodeURIComponent(investigationId)}`
    : `?limit=${encodeURIComponent(request.nextUrl.searchParams.get("limit") ?? "20")}&offset=0`;
  try {
    return relay(await fetch(`${API_BASE_URL}/investigations${query}`, { cache: "no-store" }));
  } catch {
    return safeError(502, "Could not reach the StockWise investigation API.");
  }
}

export async function POST(request: NextRequest) {
  if (!request.headers.get("origin") || request.headers.get("origin") !== request.nextUrl.origin) {
    return safeError(403, "Investigation requests must come from this local StockWise page.");
  }

  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return safeError(400, "The investigation request must contain valid JSON.");
  }

  try {
    const response = await fetch(`${API_BASE_URL}/investigations`, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(180_000),
    });
    return relay(response);
  } catch {
    return safeError(502, "The investigation request did not complete. Check FastAPI and local model readiness.");
  }
}
