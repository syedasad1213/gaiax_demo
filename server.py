#!/usr/bin/env python3
"""
Minimal local server for the front end (standard library only).

    python server.py            # then open http://127.0.0.1:8000

Serves ./frontend and exposes POST /api/check (raw JSON file as request body).
dpp_checker.py is used unchanged.
"""
import json
import os
import tempfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from dpp_checker import CheckerConfig, report_to_dict, run_compliance_checks

ROOT = Path(__file__).parent / "frontend"
MAX_BYTES = 10 * 1024 * 1024
HOST, PORT = "127.0.0.1", 8000


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.split("?")[0] != "/api/check":
            return self._json(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._json(411, {"error": "Content-Length required"})
        if length > MAX_BYTES:
            return self._json(413, {"error": "File too large (max 10 MB)"})

        data = self.rfile.read(length)
        name = unquote(self.headers.get("X-Filename", "uploaded.json"))[:200]
        fd, tmp = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            report = run_compliance_checks(tmp, CheckerConfig())
            report.path = name
            out = report_to_dict(report)
            for r in out["results"]:                 # don't leak the temp path
                if r["location"] == tmp:
                    r["location"] = name
            self._json(200, out)
        except Exception as exc:                     # pragma: no cover
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            os.unlink(tmp)


if __name__ == "__main__":
    print(f"DPP checker UI: http://{HOST}:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
