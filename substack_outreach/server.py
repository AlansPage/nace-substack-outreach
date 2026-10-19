"""Same-origin loopback-only API and static frontend. No generic file server or CORS."""
from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from .common import FITS, STATUSES
from .config import ROOT
from .evidence import ASKS, prepare_evidence
from .pipeline import Pipeline


def serve(config, port=8765):
    pipeline = Pipeline(config)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # URLs can contain contact data. Avoid request/access logs.
            pass

        def headers_ok(self, mutation=False):
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in hosts:
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + h for h in hosts}:
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return False
            return not mutation or self.headers.get("X-Local-Token") == token

        def send(self, value, status=200, content_type="application/json; charset=utf-8"):
            body = value if isinstance(value, bytes) else (value.encode("utf-8") if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode("utf-8"))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.headers_ok():
                return self.send({"error": "Local same-origin access only"}, 403)
            u = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/api/substack/bootstrap":
                    facts, _ = pipeline.facts()
                    return self.send({"token": token, "statuses": STATUSES, "fits": FITS, "asks": ASKS,
                                      "provider": config["provider"], "sender": config["sender"], "facts": facts,
                                      "network_enabled": config["network_enabled"], "batch_size": config["batch_size"]})
                if u.path == "/api/substack/queue":
                    return self.send(pipeline.queue(query.get("scope", "pilot"), query.get("search", ""), query.get("status", ""), int(query.get("limit", 100)), int(query.get("offset", 0))))
                if u.path == "/api/substack/metrics":
                    return self.send(pipeline.store.metrics())
                if u.path == "/api/substack/export":
                    fmt = query.get("format", "json")
                    return self.send(pipeline.export(fmt, query.get("scope", "pilot")), content_type="text/csv; charset=utf-8" if fmt == "csv" else "application/json; charset=utf-8")
                if u.path.startswith("/api/substack/publication/"):
                    pub = pipeline.detail(int(u.path.rsplit("/", 1)[1]))
                    pub["evidence_candidate"] = prepare_evidence(pub)
                    return self.send(pub)
                # Explicit allowlist: workbook, SQLite, exports, config and environment files are never served.
                static = {"/": ROOT / "public/index.html", "/index.html": ROOT / "public/index.html"}
                if u.path in static and static[u.path].is_file():
                    return self.send(static[u.path].read_bytes(), content_type="text/html; charset=utf-8")
                return self.send({"error": "Not found. Run py -3.12 -X utf8 build.py first."}, 404)
            except (ValueError, KeyError, OSError) as exc:
                return self.send({"error": str(exc)}, 400)

        def do_POST(self):
            if not self.headers_ok(mutation=True):
                return self.send({"error": "Local session token and same-origin request required"}, 403)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 250_000 or self.headers.get_content_type() != "application/json":
                    raise ValueError("Expected JSON body, maximum 250 KB")
                data = json.loads(self.rfile.read(size))
                parts = urlsplit(self.path).path.strip("/").split("/")
                if len(parts) != 5 or parts[:3] != ["api", "substack", "publication"]:
                    return self.send({"error": "Unknown action"}, 404)
                return self.send(pipeline.action(int(parts[3]), parts[4], data))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                return self.send({"error": str(exc)}, 400)

    print(f"Local review: http://127.0.0.1:{port}/#substack", flush=True)
    print("SQLite and keys stay in the local worker. Ctrl+C stops the server.", flush=True)
    server = HTTPServer(("127.0.0.1", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        pipeline.store.close()
