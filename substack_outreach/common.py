from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

STATUSES = ["Imported", "Shortlisted", "Needs research", "Researched", "Drafted",
            "Reviewed", "Approved", "Sent", "Replied", "Skipped", "Do not contact"]
FITS = ["Strong technical fit", "Relevant industry/application fit",
        "Exploratory editorial fit", "Unrelated", "Insufficient information"]
TECH = ["AI engineering", "AI agents", "agentic workflows", "machine learning", "LLM",
        "small language models", "inference", "model evaluation", "classification",
        "intent detection", "routing", "tool selection", "structured outputs", "computer use",
        "browser agents", "multimodal models", "n8n", "workflow automation",
        "document processing", "OCR", "RAG", "developer tools"]
APPLICATION = ["artificial intelligence", "generative AI", "automation", "AI applications", "AI tools"]
EDITORIAL = ["digital life", "digital habits", "attention economy", "philosophy of technology",
             "digital culture", "human agency", "digital minimalism"]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value):
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def norm(value):
    return " ".join(str(value or "").split())


def safe_url(value):
    try:
        u = urlsplit(str(value or "").strip())
        if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password or u.port not in (None, 80, 443):
            return ""
        if any(ord(c) < 33 for c in str(value)):
            return ""
        return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path or "", u.query, ""))
    except ValueError:
        return ""


def canonical(value):
    u = urlsplit(safe_url(value))
    if not u.hostname:
        return ""
    return urlunsplit(("https", u.hostname.removeprefix("www."), u.path.rstrip("/"), "", ""))


def is_profile(value):
    u = urlsplit(canonical(value))
    return u.hostname == "substack.com" and (u.path.startswith("/@") or u.path.startswith("/profile/"))


def publication_url(value):
    u = canonical(value)
    # A Substack identity or reader route is not evidence of a publication domain.
    return "" if not u or urlsplit(u).hostname == "substack.com" else u


def matches(text, terms):
    return [t for t in terms if re.search(r"(?<!\w)" + re.escape(t).replace(r"\ ", r"[ -]") + r"s?(?!\w)", text, re.I)]


def classify(source):
    text = " ".join(str(source.get(k) or "") for k in ("name", "hero_text", "description", "categories", "author_bio"))
    tech, app, editorial = matches(text, TECH), matches(text, APPLICATION), matches(text, EDITORIAL)
    descriptive = " ".join(str(source.get(k) or "") for k in ("hero_text", "description", "author_bio"))
    # Strong requires multiple technical clues, at least one in descriptive prose.
    if len(tech) >= 2 and matches(descriptive, TECH):
        fit, score = FITS[0], 100 + len(tech)
    elif tech or app:
        fit, score = FITS[1], 50 + len(tech) + len(app)
    elif editorial:
        fit, score = FITS[2], 10 + len(editorial)
    else:
        fit, score = (FITS[4] if len(norm(descriptive)) < 30 else FITS[3]), 0
    reasons = ["Local metadata signals: " + ", ".join(tech + app + editorial)] if tech + app + editorial else ["No specific connection in available metadata."]
    reasons.append("Provisional triage only; article evidence and human review are required. Subscriber estimates and first_post_date do not establish fit or recent activity.")
    return fit, score, reasons


def html_email(body):
    # Never render model-generated HTML. Escape all text, then link safe URLs only.
    parts = re.split(r"(https?://[^\s<>\"()—]+)", body)
    out = []
    for part in parts:
        url = part.rstrip(".,;)") if part.startswith(("https://", "http://")) else ""
        if safe_url(url):
            out.append('<a href="' + html.escape(url, quote=True) + '" rel="noopener noreferrer">' + html.escape(url) + '</a>' + html.escape(part[len(url):]))
        else:
            out.append(html.escape(part))
    return "<p>" + "".join(out).replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
