"""Vercel entry point: Google sign-in, session-bound CSRF tokens, then the same routes as the local worker.

Every request opens its own database connection and attributes events to the signed-in email.
Nothing here sends email; the static page is served by Vercel from public/.
"""
from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from . import auth
from .config import load_hosted_config
from .pipeline import Pipeline
from .server import JSON, SECURITY_HEADERS, api_get, api_post, body_limit, encode


def request_path(raw_path):
    """Route path and query. Vercel may hand us /api/index?path=... after the rewrite in vercel.json."""
    u = urlsplit(raw_path)
    query = {k: v[0] for k, v in parse_qs(u.query).items()}
    path = u.path
    if path.rstrip("/") in ("/api/index", "/api/index.py", "/api") and "path" in query:
        path = "/api/" + query.pop("path").lstrip("/")
    return path, query


class handler(BaseHTTPRequestHandler):
    environ = os.environ

    def log_message(self, fmt, *args):
        # URLs and bodies can contain contact data. Avoid request logs.
        pass

    def reply(self, value, status=200, content_type=JSON, cookies=(), location=None, headers=None):
        body = encode(value)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, header in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(name, header)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(body)

    def settings(self):
        return auth.AuthSettings.from_env(self.environ)

    def do_GET(self):
        try:
            settings = self.settings()
        except ValueError as exc:
            return self.reply({"error": "Server configuration: " + str(exc)}, 500)
        path, query = request_path(self.path)
        if path == "/api/auth/login":
            url, flow = auth.login_redirect(settings)
            return self.reply(b"", 302, "text/plain", [auth.set_cookie(auth.FLOW_COOKIE, flow, auth.FLOW_SECONDS)], url)
        if path == "/api/auth/callback":
            return self.callback(settings, query)
        if path == "/api/auth/logout":
            return self.reply(b"", 302, "text/plain", [auth.set_cookie(auth.SESSION_COOKIE, "", 0)], "/")
        user = auth.read_session(settings, self.headers.get("Cookie"))
        if not user:
            return self.reply({"error": "Sign in required", "login": "/api/auth/login"}, 401)
        return self.with_pipeline(user, lambda pipeline, config: self.get_route(settings, pipeline, config, path, query, user))

    def get_route(self, settings, pipeline, config, path, query, user):
        status, body, content_type, *extra = api_get(pipeline, config, path, query)
        if path == "/api/substack/bootstrap" and status == 200:
            body.update(token=auth.csrf_token(settings, self.headers.get("Cookie")), user={"email": user["email"]}, hosted=True)
        return self.reply(body, status, content_type, headers=extra[0] if extra else None)

    def callback(self, settings, query):
        flow = auth.unsign(settings.session_secret, auth.cookies(self.headers.get("Cookie")).get(auth.FLOW_COOKIE))
        clear_flow = auth.set_cookie(auth.FLOW_COOKIE, "", 0)
        try:
            if query.get("error"):
                raise ValueError("Google sign-in was cancelled")
            if not flow or not query.get("state") or query["state"] != flow.get("state") or not query.get("code"):
                raise ValueError("Sign-in could not be matched to this browser; start again")
            user = auth.check_claims(settings, auth.exchange_code(settings, query["code"]), flow["nonce"])
        except ValueError as exc:
            return self.reply(str(exc) + "\nOpen the workspace and sign in again.", 400, "text/plain; charset=utf-8", [clear_flow])
        session = auth.set_cookie(auth.SESSION_COOKIE, auth.session_value(settings, user), auth.SESSION_SECONDS)
        return self.reply(b"", 302, "text/plain", [clear_flow, session], "/")

    def do_POST(self):
        try:
            settings = self.settings()
        except ValueError as exc:
            return self.reply({"error": "Server configuration: " + str(exc)}, 500)
        user = auth.read_session(settings, self.headers.get("Cookie"))
        if not user:
            return self.reply({"error": "Sign in required", "login": "/api/auth/login"}, 401)
        # Same-origin page, with the token bound to this session cookie.
        if self.headers.get("Origin") != settings.app_origin or self.headers.get("Sec-Fetch-Site") == "cross-site" \
                or self.headers.get("X-Local-Token") != auth.csrf_token(settings, self.headers.get("Cookie")):
            return self.reply({"error": "Same-origin request with the session token required"}, 403)
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        path, _ = request_path(self.path)
        raw = self.rfile.read(size) if 0 < size <= body_limit(path) else b""
        return self.with_pipeline(user, lambda pipeline, config: self.post_route(pipeline, path, raw))

    def post_route(self, pipeline, path, raw):
        status, body, content_type = api_post(pipeline, path, raw, self.headers.get_content_type())
        return self.reply(body, status, content_type)

    def with_pipeline(self, user, respond):
        try:
            config = load_hosted_config(self.environ)
            pipeline = Pipeline(config, actor=user["email"])
        except ValueError as exc:
            return self.reply({"error": "Server configuration: " + str(exc)}, 500)
        try:
            return respond(pipeline, config)
        finally:
            pipeline.store.close()


__all__ = ["handler", "request_path"]
