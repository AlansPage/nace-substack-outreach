"""Hosted sign-in: Google OpenID Connect (authorization code flow) and signed session cookies.

Standard library only. The ID token comes straight from Google's token endpoint over TLS in exchange
for our client secret, which Google documents as trustworthy without a signature check; its claims
(audience, issuer, expiry, nonce, verified email) are still checked here.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookies import SimpleCookie

GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
SESSION_COOKIE, FLOW_COOKIE = "ss_session", "ss_oauth"
SESSION_SECONDS, FLOW_SECONDS = 12 * 3600, 600


@dataclass(frozen=True)
class AuthSettings:
    client_id: str
    client_secret: str
    session_secret: str
    app_origin: str
    allowed_domain: str = ""
    allowed_emails: frozenset = frozenset()

    @property
    def redirect_uri(self):
        return self.app_origin + "/api/auth/callback"

    @classmethod
    def from_env(cls, environ):
        names = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "SESSION_SECRET", "APP_ORIGIN")
        missing = [n for n in names if not environ.get(n)]
        if missing:
            raise ValueError("Missing environment variables: " + ", ".join(missing))
        if len(environ["SESSION_SECRET"]) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")
        origin = environ["APP_ORIGIN"].rstrip("/")
        parts = urllib.parse.urlsplit(origin)
        if parts.scheme != "https" or not parts.hostname or parts.path or parts.query:
            raise ValueError("APP_ORIGIN must be the https origin of the deployment, e.g. https://outreach.example.vercel.app")
        emails = frozenset(e.strip().lower() for e in environ.get("ALLOWED_EMAILS", "").split(",") if e.strip())
        return cls(environ["GOOGLE_CLIENT_ID"], environ["GOOGLE_CLIENT_SECRET"], environ["SESSION_SECRET"], origin,
                   environ.get("ALLOWED_EMAIL_DOMAIN", "").strip().lower(), emails)


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret, payload):
    body = b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    mac = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return body + "." + b64(mac)


def unsign(secret, token, now=None):
    """Payload of a value made by sign(), or None when it was altered or has expired."""
    try:
        body, mac = str(token or "").split(".")
        expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(unb64(mac), expected):
            return None
        payload = json.loads(unb64(body))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) <= (now or time.time()):
        return None
    return payload


def cookies(header):
    jar = SimpleCookie()
    try:
        jar.load(header or "")
    except Exception:
        return {}
    return {k: v.value for k, v in jar.items()}


def set_cookie(name, value, max_age):
    return f"{name}={value}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age={max_age}"


def login_redirect(settings, now=None):
    """Google authorization URL plus the short-lived cookie that binds state and nonce to this browser."""
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    flow = sign(settings.session_secret, {"state": state, "nonce": nonce, "exp": int((now or time.time()) + FLOW_SECONDS)})
    params = {"client_id": settings.client_id, "response_type": "code", "scope": "openid email",
              "redirect_uri": settings.redirect_uri, "state": state, "nonce": nonce, "prompt": "select_account"}
    if settings.allowed_domain:
        params["hd"] = settings.allowed_domain
    return GOOGLE_AUTHORIZE + "?" + urllib.parse.urlencode(params), flow


def exchange_code(settings, code, opener=None):
    """Claims of the ID token Google returns for this authorization code."""
    data = urllib.parse.urlencode({"code": code, "client_id": settings.client_id, "client_secret": settings.client_secret,
                                   "redirect_uri": settings.redirect_uri, "grant_type": "authorization_code"}).encode()
    request = urllib.request.Request(GOOGLE_TOKEN, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with (opener or urllib.request.build_opener()).open(request, timeout=15) as response:
            result = json.loads(response.read(100_001))
        return json.loads(unb64(result["id_token"].split(".")[1]))
    except Exception:
        # Never echo Google's response: it can contain tokens.
        raise ValueError("Google sign-in failed while exchanging the authorization code") from None


def check_claims(settings, claims, nonce, now=None):
    now = now or time.time()
    if claims.get("aud") != settings.client_id or claims.get("iss") not in ISSUERS:
        raise ValueError("Sign-in token was not issued for this app")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= now:
        raise ValueError("Sign-in token expired; sign in again")
    if not nonce or not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ValueError("Sign-in could not be matched to this browser; sign in again")
    email = str(claims.get("email") or "").lower()
    if not email or claims.get("email_verified") is not True:
        raise ValueError("Google did not confirm an email address for this account")
    if settings.allowed_domain and claims.get("hd") != settings.allowed_domain:
        raise ValueError("This workspace only accepts accounts from " + settings.allowed_domain)
    if settings.allowed_emails and email not in settings.allowed_emails:
        raise ValueError("This account is not on the workspace's sign-in list")
    return {"sub": str(claims["sub"]), "email": email}


def session_value(settings, user, now=None):
    return sign(settings.session_secret, {"sub": user["sub"], "email": user["email"], "exp": int((now or time.time()) + SESSION_SECONDS)})


def read_session(settings, cookie_header, now=None):
    payload = unsign(settings.session_secret, cookies(cookie_header).get(SESSION_COOKIE), now)
    return {"sub": payload["sub"], "email": payload["email"]} if payload and payload.get("email") else None


def csrf_token(settings, cookie_header):
    """Mutation token bound to this session cookie; sent by the UI in X-Local-Token."""
    session = cookies(cookie_header).get(SESSION_COOKIE, "")
    return b64(hmac.new(settings.session_secret.encode("utf-8"), ("csrf:" + session).encode("utf-8"), hashlib.sha256).digest())
