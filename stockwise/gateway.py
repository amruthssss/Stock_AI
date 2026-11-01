"""Local allowlist gateway for the StockWise integration API."""

from __future__ import annotations

import argparse
import logging
import os
import re
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("stockwise.gateway")
UPSTREAM = "http://127.0.0.1:8000"
MAX_BODY_BYTES = 1_048_576
UPSTREAM_TIMEOUT_SECONDS = 15
_REPORT_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_ALLOWED_ROUTES = (
    (re.compile(r"^/api/v1/integrations/reports$"), frozenset({"POST"})),
    (
        re.compile(
            rf"^/api/v1/integrations/reports/(daily_inventory|weekly_data_science)/{_REPORT_ID}$"
        ),
        frozenset({"GET"}),
    ),
    (
        re.compile(r"^/api/v1/integrations/inventory-alerts/(stockout|expiry|overstock)$"),
        frozenset({"GET"}),
    ),
)
_FORWARDED_HEADERS = frozenset(
    {"accept", "authorization", "content-type", "idempotency-key", "user-agent"}
)
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def _route_method(path: str, method: str) -> str:
    for pattern, methods in _ALLOWED_ROUTES:
        if pattern.fullmatch(path):
            return "allowed" if method in methods else "method-not-allowed"
    return "not-found"


def _upstream_url(path: str, query: str, upstream: str) -> str:
    return f"{upstream.rstrip('/')}{path}" + (f"?{query}" if query else "")


def _handler_factory(
    upstream: str,
    timeout: float,
    max_body_bytes: int,
    opener: Callable[..., object] = urlopen,
) -> type[BaseHTTPRequestHandler]:
    class GatewayHandler(BaseHTTPRequestHandler):
        server_version = "StockWiseGateway"
        sys_version = ""

        def _reject(self, status: int, message: str, *, allow: str | None = None) -> None:
            body = f'{{"detail":"{message}"}}'.encode()
            self.send_response(status)
            if allow:
                self.send_header("Allow", allow)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _forward(self) -> None:
            split = urlsplit(self.path)
            route_status = _route_method(split.path, self.command)
            if route_status == "not-found":
                self._reject(404, "Not found")
                return
            if route_status == "method-not-allowed":
                allowed = next(
                    ",".join(sorted(methods))
                    for pattern, methods in _ALLOWED_ROUTES
                    if pattern.fullmatch(split.path)
                )
                self._reject(405, "Method not allowed", allow=allowed)
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._reject(400, "Invalid content length")
                return
            if content_length > max_body_bytes:
                self._reject(413, "Request body is too large")
                return
            body = self.rfile.read(content_length) if content_length else None
            headers = {
                name: self.headers[name]
                for name in _FORWARDED_HEADERS
                if self.headers.get(name) is not None
            }
            request = Request(
                _upstream_url(split.path, split.query, upstream),
                data=body,
                headers=headers,
                method=self.command,
            )
            try:
                with opener(request, timeout=timeout) as response:
                    response_body = response.read(max_body_bytes + 1)
                    status = response.status
                    response_headers = response.headers.items()
            except HTTPError as error:
                response_body = error.read(max_body_bytes + 1)
                status = error.code
                response_headers = error.headers.items()
            except (TimeoutError, URLError):
                self._reject(504, "Backend unavailable")
                return
            if len(response_body) > max_body_bytes:
                self._reject(502, "Backend response is too large")
                return
            self.send_response(status)
            for name, value in response_headers:
                if name.lower() not in _HOP_BY_HOP_HEADERS and name.lower() != "content-length":
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
            LOGGER.info("%s %s -> %s", self.command, split.path, status)

        do_GET = _forward
        do_POST = _forward

        def log_message(self, format: str, *args: object) -> None:
            LOGGER.info("%s", format % args)

    return GatewayHandler


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8010,
    upstream: str = UPSTREAM,
    timeout: float = UPSTREAM_TIMEOUT_SECONDS,
    max_body_bytes: int = MAX_BODY_BYTES,
    opener: Callable[..., object] = urlopen,
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(
        (host, port), _handler_factory(upstream, timeout, max_body_bytes, opener)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local StockWise integration gateway.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--upstream", default=UPSTREAM)
    parser.add_argument("--pid-file", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = create_server(host=args.host, port=args.port, upstream=args.upstream)
    if args.pid_file:
        args.pid_file.write_text(str(os.getpid()), encoding="ascii")
    try:
        LOGGER.info("Gateway listening on %s:%s", args.host, args.port)
        server.serve_forever()
    finally:
        server.server_close()
        if args.pid_file:
            args.pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
