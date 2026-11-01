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
  const jobId = request.nextUrl.searchParams.get("job_id");
  const apiPath = jobId
    ? `/jobs/${encodeURIComponent(jobId)}`
    : "/decision-lab/latest";
  try {
    const response = await fetch(`${API_BASE_URL}${apiPath}`, { cache: "no-store" });
    return relay(response);
  } catch {
    return safeError(502, "Could not reach the StockWise API. Check that FastAPI is running.");
  }
}

export async function POST(request: NextRequest) {
  const origin = request.headers.get("origin");
  if (!origin || origin !== request.nextUrl.origin) {
    return safeError(403, "Scenario requests must come from this local StockWise page.");
  }

  const authorization = request.headers.get("authorization");
  const idempotencyKey = request.headers.get("idempotency-key");
  if (!authorization?.startsWith("Bearer ") || !idempotencyKey) {
    return safeError(401, "An admin token and idempotency key are required to run a scenario.");
  }

  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return safeError(400, "The scenario request must contain valid JSON.");
  }

  try {
    const response = await fetch(`${API_BASE_URL}/decision-lab/runs`, {
      method: "POST",
      cache: "no-store",
      headers: {
        Authorization: authorization,
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(payload),
    });
    return relay(response);
  } catch {
    return safeError(502, "Could not reach the StockWise API. The scenario result was not confirmed.");
  }
}
