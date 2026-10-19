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
        "document processing", "OCR", "RAG", "developer tools",
        "ML", "deep learning", "agents", "data science"]
APPLICATION = ["artificial intelligence", "generative AI", "automation", "AI applications", "AI tools",
               "AI", "A.I.", "GPT", "ChatGPT"]
# Short acronyms are ordinary words in lower case ("ai" in French/Italian, "ml" for millilitres).
CASED = {"AI", "A.I.", "ML", "GPT"}
CATEGORY_WEIGHT = {"technology": 5}
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
    # Longest terms first; a shorter term inside an earlier match ("agents" in "AI agents") is not a second signal.
    text, found = str(text), set()
    for t in sorted(terms, key=len, reverse=True):
        body = re.escape(t).replace(r"\ ", r"[ -]") + "s?"
        if t in CASED:
            # ASCII edges so "AIとWeb3" and "AI活用" match while "OpenAI" and "HTML" do not.
            pattern = re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])")
        else:
            pattern = re.compile(r"(?<!\w)" + body + r"(?!\w)", re.I)
        if pattern.search(text):
            found.add(t)
            text = pattern.sub(lambda m: "\0" * len(m.group()), text)
    return [t for t in terms if t in found]


def category_weight(source):
    return sum(CATEGORY_WEIGHT.get(c.strip().lower(), 0) for c in str(source.get("categories") or "").split(";"))


def classify(source):
    text = " ".join(str(source.get(k) or "") for k in ("name", "hero_text", "description", "categories", "author_bio"))
    found = matches(text, TECH + APPLICATION + EDITORIAL)
    tech, app, editorial = ([t for t in found if t in group] for group in (TECH, APPLICATION, EDITORIAL))
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
    # The category only ranks publications that already have a topical signal; alone it is not fit evidence.
    weight = category_weight(source) if score else 0
    score += weight
    reasons = ["Local metadata signals: " + ", ".join(tech + app + editorial)] if tech + app + editorial else ["No specific connection in available metadata."]
    if weight:
        reasons.append(f"Category weight +{weight}: {norm(source.get('categories'))}")
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
