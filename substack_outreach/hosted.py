"""Hosted workspace on Vercel: Google sign-in, session-bound CSRF tokens, then the same routes as the local worker.

handle() does the work for one request. Vercel loads the WSGI `app` from the root app.py; `handler`
(a BaseHTTPRequestHandler) runs the same logic in-process for tests. Every request opens its own
database connection and attributes events to the signed-in email. Nothing here sends email.
"""
from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from . import auth
from .config import load_hosted_config
from .page import render_page
from .pipeline import Pipeline
from .server import JSON, SECURITY_HEADERS, api_get, api_post, body_limit, encode

HTML = "text/html; charset=utf-8"
_page = None


def page():
    global _page
    if _page is None:
        _page = render_page()
    return _page


def request_path(raw_path):
    """Route path and query. Older deployments rewrote /api/* to /api/index?path=...; accept both."""
    u = urlsplit(raw_path)
    query = {k: v[0] for k, v in parse_qs(u.query).items()}
    path = u.path
    if path.rstrip("/") in ("/api/index", "/api/index.py", "/api") and "path" in query:
        path = "/api/" + query.pop("path").lstrip("/")
    return path, query


class Headers:
    """Case-insensitive request headers, whichever server delivered them."""

    def __init__(self, pairs):
        self.values = {k.lower(): v for k, v in pairs}

    def get(self, name, default=None):
        return self.values.get(name.lower(), default)

    def content_type(self):
        return str(self.get("Content-Type") or "").split(";")[0].strip().lower()


def response(value, status=200, content_type=JSON, cookies=(), location=None, headers=None):
    pairs = [("Content-Type", content_type)] + list({**SECURITY_HEADERS, **(headers or {})}.items())
    pairs += [("Set-Cookie", c) for c in cookies] + ([("Location", location)] if location else [])
    return status, pairs, encode(value)


def handle(environ, method, raw_path, headers, read_body):
    """One request -> (status, header pairs, body bytes). read_body(limit) returns at most `limit` bytes."""
    path, query = request_path(raw_path)
    if method == "GET" and path in ("/", "/index.html"):
        return response(page(), 200, HTML)
    if not path.startswith("/api/"):
        return response({"error": "Not found"}, 404)
    try:
        settings = auth.AuthSettings.from_env(environ)
    except ValueError as exc:
        return response({"error": "Server configuration: " + str(exc)}, 500)
    cookie = headers.get("Cookie")
    if method == "GET" and path == "/api/auth/login":
        url, flow = auth.login_redirect(settings)
        return response(b"", 302, "text/plain", [auth.set_cookie(auth.FLOW_COOKIE, flow, auth.FLOW_SECONDS)], url)
    if method == "GET" and path == "/api/auth/callback":
        return callback(settings, cookie, query)
    if method == "GET" and path == "/api/auth/logout":
        return response(b"", 302, "text/plain", [auth.set_cookie(auth.SESSION_COOKIE, "", 0)], "/")
    user = auth.read_session(settings, cookie)
    if not user:
        return response({"error": "Sign in required", "login": "/api/auth/login"}, 401)
    if method == "GET":
        return with_pipeline(environ, user, lambda pipeline, config: get_route(settings, cookie, pipeline, config, path, query, user))
    if method != "POST":
        return response({"error": "Method not allowed"}, 405)
    # Same-origin page, with the token bound to this session cookie.
    if headers.get("Origin") != settings.app_origin or headers.get("Sec-Fetch-Site") == "cross-site" \
            or headers.get("X-Local-Token") != auth.csrf_token(settings, cookie):
        return response({"error": "Same-origin request with the session token required"}, 403)
    try:
        size = int(headers.get("Content-Length") or 0)
    except ValueError:
        size = 0
    raw = read_body(size) if 0 < size <= body_limit(path) else b""
    content_type = headers.content_type()
    return with_pipeline(environ, user, lambda pipeline, config: post_route(pipeline, path, raw, content_type))


def get_route(settings, cookie, pipeline, config, path, query, user):
    status, body, content_type, *extra = api_get(pipeline, config, path, query)
    if path == "/api/substack/bootstrap" and status == 200:
        body.update(token=auth.csrf_token(settings, cookie), user={"email": user["email"]}, hosted=True)
    return response(body, status, content_type, headers=extra[0] if extra else None)


def post_route(pipeline, path, raw, content_type):
    status, body, kind = api_post(pipeline, path, raw, content_type)
    return response(body, status, kind)


def callback(settings, cookie, query):
    flow = auth.unsign(settings.session_secret, auth.cookies(cookie).get(auth.FLOW_COOKIE))
    clear_flow = auth.set_cookie(auth.FLOW_COOKIE, "", 0)
    try:
        if query.get("error"):
            raise ValueError("Google sign-in was cancelled")
        if not flow or not query.get("state") or query["state"] != flow.get("state") or not query.get("code"):
            raise ValueError("Sign-in could not be matched to this browser; start again")
        user = auth.check_claims(settings, auth.exchange_code(settings, query["code"]), flow["nonce"])
    except ValueError as exc:
        return response(str(exc) + "\nOpen the workspace and sign in again.", 400, "text/plain; charset=utf-8", [clear_flow])
    session = auth.set_cookie(auth.SESSION_COOKIE, auth.session_value(settings, user), auth.SESSION_SECONDS)
    return response(b"", 302, "text/plain", [clear_flow, session], "/")


def with_pipeline(environ, user, respond):
    try:
        config = load_hosted_config(environ)
        pipeline = Pipeline(config, actor=user["email"])
    except ValueError as exc:
        return response({"error": "Server configuration: " + str(exc)}, 500)
    try:
        return respond(pipeline, config)
    finally:
        pipeline.store.close()


REASONS = {200: "OK", 302: "Found", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
           404: "Not Found", 405: "Method Not Allowed", 500: "Internal Server Error"}


def app(environ, start_response):
    """WSGI entry point that Vercel loads (see app.py)."""
    pairs = [(k[5:].replace("_", "-"), v) for k, v in environ.items() if k.startswith("HTTP_")]
    pairs += [(name, environ[key]) for key, name in (("CONTENT_TYPE", "Content-Type"), ("CONTENT_LENGTH", "Content-Length")) if environ.get(key)]
    raw_path = environ.get("PATH_INFO") or "/"
    if environ.get("QUERY_STRING"):
        raw_path += "?" + environ["QUERY_STRING"]
    status, headers, body = handle(os.environ, environ.get("REQUEST_METHOD", "GET"), raw_path, Headers(pairs),
                                   lambda limit: environ["wsgi.input"].read(limit))
    start_response(f"{status} {REASONS.get(status, 'OK')}", headers + [("Content-Length", str(len(body)))])
    return [body]


class handler(BaseHTTPRequestHandler):
    """The same logic behind Python's built-in HTTP server (used by the tests)."""
    environ = os.environ

    def log_message(self, fmt, *args):
        # URLs and bodies can contain contact data. Avoid request logs.
        pass

    def run(self, method):
        status, headers, body = handle(self.environ, method, self.path, Headers(self.headers.items()), self.rfile.read)
        self.send_response(status)
        for name, value in headers + [("Content-Length", str(len(body)))]:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.run("GET")

    def do_POST(self):
        self.run("POST")


__all__ = ["app", "handler", "handle", "request_path"]
