"""Same-origin loopback-only API and static frontend. No generic file server or CORS.

api_get/api_post are the routes shared with the hosted handler (hosted.py); callers authenticate first.
"""
from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from .common import FITS, STATUSES
from .config import ROOT
from .evidence import ASKS, prepare_evidence
from .common import now
from .pipeline import CONTACT_CHANNELS, Pipeline, render_export

JSON = "application/json; charset=utf-8"
SECURITY_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}
MAX_BODY = 250_000
# Uploaded tables arrive in batches; Vercel caps a request at 4.5 MB.
UPLOAD_MAX_BODY = 4_000_000


def body_limit(path):
    return UPLOAD_MAX_BODY if path.rstrip("/").endswith("/api/substack/upload") else MAX_BODY


def api_get(pipeline, config, path, query):
    """(status, body, content type[, extra headers]) for a read route. bootstrap callers add their own token and user."""
    try:
        if path == "/api/substack/bootstrap":
            facts, _ = pipeline.facts()
            return 200, {"statuses": STATUSES, "fits": FITS, "asks": ASKS, "provider": config["provider"],
                         "sender": config["sender"], "facts": facts, "network_enabled": config["network_enabled"],
                         "batch_size": config["batch_size"], "collect_contacts": config["collect_contacts"],
                         "contact_channels": CONTACT_CHANNELS, "channels": pipeline.channel_counts()}, JSON
        if path == "/api/substack/queue":
            return 200, pipeline.queue(query.get("scope", "pilot"), query.get("search", ""), query.get("status", ""),
                                       int(query.get("limit", 100)), int(query.get("offset", 0)), query.get("channel", "substack")), JSON
        if path == "/api/substack/metrics":
            return 200, pipeline.store.metrics(), JSON
        if path == "/api/substack/export":
            fmt, scope = query.get("format", "json"), query.get("scope", "pilot")
            if fmt not in ("csv", "json"):
                raise ValueError("The worker exports csv or json; build the send sheet with the CLI")
            if scope not in ("pilot", "shortlisted", "all"):
                raise ValueError("Export scope must be pilot, shortlisted or all")
            channel = query.get("channel") or None
            if channel and channel not in ("substack", "medium", "discord"):
                raise ValueError("Export channel must be substack, medium or discord")
            rows = pipeline.export_rows(scope, channel)
            # A real file download: works as a plain link and tells the page what it contains.
            headers = {"Content-Disposition": f'attachment; filename="{channel or ('substack' if scope in ('pilot', 'shortlisted') else 'all')}-{scope}-{now()[:10]}.{fmt}"',
                       "X-Export-Rows": str(len(rows)), "X-Export-Drafts": str(sum(1 for r in rows if r["email_body"]))}
            return 200, render_export(rows, fmt), "text/csv; charset=utf-8" if fmt == "csv" else JSON, headers
        if path.startswith("/api/substack/publication/"):
            pub = pipeline.detail(int(path.rsplit("/", 1)[1]))
            pub["evidence_candidate"] = prepare_evidence(pub)
            return 200, pub, JSON
        return 404, {"error": "Not found"}, JSON
    except (ValueError, KeyError, OSError) as exc:
        return 400, {"error": str(exc)}, JSON


def api_post(pipeline, path, raw, content_type):
    try:
        if not 0 < len(raw) <= body_limit(path) or content_type != "application/json":
            raise ValueError(f"Expected JSON body, maximum {body_limit(path) // 1000} KB")
        data = json.loads(raw)
        parts = path.strip("/").split("/")
        if parts == ["api", "substack", "upload"]:
            return 200, pipeline.upload_rows(data), JSON
        if parts == ["api", "substack", "collect-contacts"]:
            return 200, {"results": pipeline.collect_contacts(max(0, int(data.get("batch") or 0)) or None, bool(data.get("retry_failed")))}, JSON
        if len(parts) != 5 or parts[:3] != ["api", "substack", "publication"]:
            return 404, {"error": "Unknown action"}, JSON
        return 200, pipeline.action(int(parts[3]), parts[4], data), JSON
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return 400, {"error": str(exc)}, JSON


def encode(value):
    return value if isinstance(value, bytes) else (value.encode("utf-8") if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode("utf-8"))


def serve(config, port=8765):
    # One local operator: their configured sender name is who acted.
    pipeline = Pipeline(config, actor=config["sender"]["name"])
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

        def send(self, value, status=200, content_type=JSON, headers=None):
            body = encode(value)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, header in {**SECURITY_HEADERS, **(headers or {})}.items():
                self.send_header(name, header)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.headers_ok():
                return self.send({"error": "Local same-origin access only"}, 403)
            u = urlsplit(self.path)
            # Explicit allowlist: workbook, SQLite, exports, config and environment files are never served.
            static = {"/": ROOT / "public/index.html", "/index.html": ROOT / "public/index.html"}
            if u.path in static:
                if static[u.path].is_file():
                    return self.send(static[u.path].read_bytes(), content_type="text/html; charset=utf-8")
                return self.send({"error": "Not found. Run python build.py first."}, 404)
            status, body, content_type, *extra = api_get(pipeline, config, u.path, {k: v[0] for k, v in parse_qs(u.query).items()})
            if u.path == "/api/substack/bootstrap" and status == 200:
                body["token"] = token
            return self.send(body, status, content_type, extra[0] if extra else None)

        def do_POST(self):
            if not self.headers_ok(mutation=True):
                return self.send({"error": "Local session token and same-origin request required"}, 403)
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                size = 0
            path = urlsplit(self.path).path
            raw = self.rfile.read(size) if 0 < size <= body_limit(path) else b""
            status, body, content_type = api_post(pipeline, path, raw, self.headers.get_content_type())
            return self.send(body, status, content_type)

    print(f"Local review: http://127.0.0.1:{port}/", flush=True)
    print("SQLite and keys stay in the local worker. Ctrl+C stops the server.", flush=True)
    server = HTTPServer(("127.0.0.1", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        pipeline.store.close()
