"""Tiny loopback-only HTTP server for the browser testing example."""

import argparse
import json
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
CATALOG = [
    {
        "id": "field-notes",
        "name": "Field Notes Set",
        "description": "Three recycled-stock notebooks for weather, routes, and sketches.",
        "price": 18,
        "category": "Desk",
        "mark": "FN",
    },
    {
        "id": "alpine-mug",
        "name": "Alpine Mug",
        "description": "A speckled stoneware cup with a wide, steady trail-side handle.",
        "price": 24,
        "category": "Camp",
        "mark": "AM",
    },
    {
        "id": "signal-tote",
        "name": "Signal Tote",
        "description": "Heavy canvas, bright straps, and room for a market-day haul.",
        "price": 32,
        "category": "Carry",
        "mark": "ST",
    },
]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/health":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        if path == "/api/catalog":
            payload = json.dumps({"products": CATALOG}, separators=(",", ":")).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/":
            self.path = "/index.html"
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
