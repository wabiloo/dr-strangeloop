"""Tiny static server for the harness page and the cached player SDKs."""

from __future__ import annotations

import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import paths

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")


def _safe_join(root: Path, rel: str) -> Path | None:
    p = (root / rel.lstrip("/")).resolve()
    return p if p == root.resolve() or root.resolve() in p.parents else None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    vendor_root: Path

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            path = "/index.html"
        if path.startswith("/vendor/"):
            f = _safe_join(self.vendor_root, path[len("/vendor/"):])
        else:
            f = _safe_join(paths.HARNESS_DIR, path)
        if f is None or not f.is_file():
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(f.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class HarnessServer:
    def __init__(self, vendor_root: Path | None = None):
        handler = type("H", (_Handler,), {"vendor_root": vendor_root or paths.vendor_root()})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
