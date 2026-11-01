import { NextRequest, NextResponse } from "next/server";

const API_BASE_URL = (
  process.env.STOCKWISE_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1"
).replace(/\/+$/, "");
const REPORT_TYPES = new Set(["daily_inventory", "weekly_data_science"]);
const SAFE_REPORT_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/;

function safeError(status: number, message: string) {
  return NextResponse.json({ message }, { status, headers: { "Cache-Control": "no-store" } });
}

async function relay(response: Response) {
  const headers = new Headers({
    "Content-Type": response.headers.get("content-type") ?? "application/octet-stream",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  const disposition = response.headers.get("content-disposition");
  if (disposition) headers.set("Content-Disposition", disposition);
  return new NextResponse(await response.arrayBuffer(), { status: response.status, headers });
}

export async function GET(request: NextRequest) {
  const params = request.nextUrl.searchParams;
  const reportType = params.get("report_type");
  const reportId = params.get("report_id");
  const format = params.get("format");
  let apiPath: string;

  if (!reportType && !reportId && !format) {
    const limit = Number(params.get("limit") ?? "50");
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
      return safeError(400, "Report list limit must be between one and one hundred.");
    }
    apiPath = `/reports?limit=${limit}&offset=0`;
  } else {
    if (!reportType || !REPORT_TYPES.has(reportType) || !reportId || !SAFE_REPORT_ID.test(reportId)) {
      return safeError(400, "A supported report type and valid report ID are required.");
    }
    if (format && format !== "json" && format !== "html") {
      return safeError(400, "Report format must be json or html.");
    }
    apiPath = `/reports/${encodeURIComponent(reportType)}/${encodeURIComponent(reportId)}`;
    if (format) apiPath += `/download?format=${format}`;
  }

  try {
    const response = await fetch(`${API_BASE_URL}${apiPath}`, { cache: "no-store" });
    return relay(response);
  } catch {
    return safeError(502, "Could not reach the StockWise API. Check that FastAPI is running.");
  }
}
