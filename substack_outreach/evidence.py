from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path

from .common import FITS, TECH, canonical, digest, dumps, matches, norm, now, safe_url

ASKS = {"review": "Would you be open to evaluating Drex Max for a possible review?",
        "technical walkthrough": "Would a technical walkthrough of this use case be useful for your publication?",
        "roundup inclusion": "Would you consider evaluating Drex Max for inclusion in a relevant roundup?",
        "exploratory discussion": "Would you be open to an exploratory discussion about which digital decisions people would choose to delegate?"}
BASE_CLAIMS = ["product.model", "product.agents", "offer.credits", "offer.questions", "link.developers"]


def registry(store, path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    required = {"id", "text", "source", "approval_status", "approved_by", "approved_at", "scope", "expiry_or_review_date"}
    if not isinstance(data.get("version"), int) or data.get("launch_status") not in ("unconfirmed", "scheduled_confirmed", "released_confirmed"):
        raise ValueError("Registry requires integer version and an explicit launch_status")
    ids = set()
    for e in data["entries"]:
        if required - e.keys() or e["id"] in ids or e["approval_status"] not in ("pending", "approved", "rejected"):
            raise ValueError("Invalid/duplicate registry entry")
        if e["approval_status"] == "approved" and not (e["approved_by"] and e["approved_at"]):
            raise ValueError("Approved registry entries require approved_by and approved_at")
        ids.add(e["id"])
    h = digest(data)
    store.db.execute("INSERT OR IGNORE INTO registry_history VALUES(?,?,?,?)", (h, data["version"], dumps(data), now()))
    # A change to facts, approval, expiry or launch status invalidates earlier approvals.
    for row in store.db.execute("SELECT d.publication_id,d.data FROM drafts d JOIN publications p ON p.id=d.publication_id WHERE d.approved_hash IS NOT NULL AND p.status IN ('Approved','Reviewed')").fetchall():
        if json.loads(row["data"]).get("registry_hash") != h:
            store.invalidate(row["publication_id"], "Facts registry changed")
    store.db.commit()
    return data, h


def prepare_evidence(pub):
    if pub.get("evidence"):
        return pub["evidence"]
    e = {"author_name": pub.get("author_name") or "", "name_source": pub.get("name_source") or "",
         "publication": pub.get("publication") or "", "publication_url": pub.get("publication_url") or "",
         "fit_category": pub["fit_category"], "article_title": "", "article_url": "", "article_date": "",
         "specific_point": "", "supporting_excerpt": "", "drex_connection": "",
         "selected_claim_ids": list(BASE_CLAIMS), "suggested_ask": "", "contact_route": pub.get("contact_route") or "",
         "contact_source_url": pub.get("contact_source_url") or "", "ready_to_draft": False,
         "review_flags": [], "evidence_reviewed_by": "", "ask_format": "", "result": "NEEDS_RESEARCH"}
    if pub["fit_category"] == FITS[3]:
        e.update(result="SKIP", review_flags=["NO_CREDIBLE_CONNECTION"])
        return e
    if pub["fit_category"] not in FITS[:2]:
        e["review_flags"] = ["EDITORIAL_ANGLE_REQUIRES_SUPPLIED_EVIDENCE"]
        return e
    for article in sorted(pub.get("articles", []), key=lambda a: a.get("date") or "", reverse=True):
        sentences = re.split(r"(?<=[.!?])\s+|\n+", article["text"])
        point = next((norm(s) for s in sentences if 8 <= len(s.split()) <= 45 and matches(s, TECH)), None)
        if point:
            e.update(article_title=article.get("title") or "", article_url=article["url"], article_date=article.get("date") or "",
                     specific_point=point, supporting_excerpt=point,
                     drex_connection="This connects to our work on evaluating where decision models fit into practical agent workflows.",
                     suggested_ask=ASKS["technical walkthrough"], ask_format="technical walkthrough",
                     ready_to_draft=True, result="EVIDENCE_CANDIDATE",
                     review_flags=["HUMAN_EVIDENCE_REVIEW_REQUIRED"])
            return e
    e["review_flags"] = ["NO_SUPPORTED_PERSONALIZATION_POINT"]
    return e


def evidence_checks(pub, e):
    flags = []
    if not e or not e.get("ready_to_draft"):
        return ["NEEDS_RESEARCH"]
    if e.get("fit_category") != pub["fit_category"] or pub["fit_category"] in FITS[3:]:
        flags.append("FIT_NOT_SUPPORTED")
    if e.get("publication_url", "") != pub.get("publication_url", ""):
        flags.append("PUBLICATION_EVIDENCE_MISMATCH")
    article = next((a for a in pub.get("articles", []) if canonical(a["url"]) == canonical(e.get("article_url"))), None)
    if not article:
        flags.append("ARTICLE_NOT_COLLECTED")
    else:
        excerpt = norm(e.get("supporting_excerpt"))
        if not excerpt or excerpt not in norm(article["text"]):
            flags.append("EXCERPT_NOT_IN_SOURCE")
        if len(excerpt.split()) > 60:
            flags.append("EXCERPT_TOO_LONG")
        if not article.get("membership_confirmed") or not article.get("relationship_source"):
            flags.append("ARTICLE_MEMBERSHIP_UNVERIFIED")
        if e.get("article_title") != (article.get("title") or "") or e.get("article_date", "") != (article.get("date") or ""):
            flags.append("ARTICLE_METADATA_MISMATCH")
        if article.get("source_kind") == "user_fixture":
            flags.append("FIXTURE_ONLY_USER_SUPPLIED_NOT_FETCHED")
        if article.get("content_status") != "complete":
            flags.append("SOURCE_EXCERPT_ONLY")
        if not article.get("date"):
            flags.append("ARTICLE_DATE_UNKNOWN")
        elif article["date"][:10] > now()[:10]:
            flags.append("ARTICLE_DATE_IN_FUTURE")
        else:
            from datetime import date
            if (date.fromisoformat(now()[:10]) - date.fromisoformat(article["date"][:10])).days > 180:
                flags.append("ARTICLE_ACTIVITY_STALE")
    if not e.get("specific_point") or not e.get("drex_connection") or e.get("ask_format") not in ASKS:
        flags.append("INCOMPLETE_ANGLE_OR_ASK")
    if pub["fit_category"] == FITS[2]:
        flags.append("EXPLORATORY_EDITORIAL_FIT")
        if e.get("ask_format") != "exploratory discussion":
            flags.append("EXPLORATORY_ASK_MISMATCH")
    if not e.get("evidence_reviewed_by"):
        flags.append("HUMAN_EVIDENCE_REVIEW_REQUIRED")
    if not (pub.get("confirmed_first_name") and pub.get("name_source")):
        flags.append("NEUTRAL_GREETING_NAME_UNCONFIRMED")
    if e.get("author_name") != (pub.get("author_name") or "") or e.get("name_source", "") != pub.get("name_source", ""):
        flags.append("NAME_EVIDENCE_MISMATCH")
    if e.get("contact_route", "") != pub.get("contact_route", "") or e.get("contact_source_url", "") != pub.get("contact_source_url", ""):
        flags.append("CONTACT_EVIDENCE_MISMATCH")
    return flags


WARNINGS = {"SOURCE_EXCERPT_ONLY", "ARTICLE_DATE_UNKNOWN", "ARTICLE_ACTIVITY_STALE", "EXPLORATORY_EDITORIAL_FIT",
            "NEUTRAL_GREETING_NAME_UNCONFIRMED", "REPETITIVE_OPENING", "HUMAN_SEMANTIC_REVIEW_REQUIRED"}


def validate(store, pub, draft, facts, facts_hash):
    e = draft.get("evidence_record") or {}
    flags = evidence_checks(pub, e)
    if pub.get('evidence'):
        without_detail = lambda value: {k: v for k, v in value.items() if k != 'email_detail'}
        if digest(without_detail(pub['evidence'])) != digest(without_detail(e)):
            flags.append('EVIDENCE_CHANGED_REGENERATE_OR_EDIT')
    if draft.get("provider") == "mock":
        flags.append("MOCK_PREVIEW_NOT_SEND_READY")
    if draft.get("registry_hash") != facts_hash:
        flags.append("REGISTRY_CHANGED_REGENERATE_OR_EDIT")
    if digest(draft.get("sender")) != digest(draft.get("current_sender", draft.get("sender"))):
        flags.append("SENDER_CHANGED")
    ids = draft.get("claim_ids_used", [])
    entries = {a["id"]: a for a in facts["entries"]}
    text = draft.get("plain_text_body", "")
    subject = draft.get("subject", "")
    style = style_checks(text, subject, e, draft.get("sender", {}))
    flags.extend(style["flags"])
    if not subject.strip() or len(subject) > 180 or "\n" in subject:
        flags.append("SUBJECT_INVALID")
    benchmarks = [entries[i] for i in ids if i in entries and entries[i]["scope"] == "benchmark"]
    if len(benchmarks) > 1:
        flags.append("TOO_MANY_BENCHMARKS")
    if benchmarks and (pub["fit_category"] == FITS[2] or not e.get("benchmark_relevance")):
        flags.append("BENCHMARK_RELEVANCE_UNCONFIRMED")
    if not set(BASE_CLAIMS).issubset(ids):
        flags.append("PRODUCT_USE_CASE_SUPPORT_OR_LINK_MISSING")
    for cid in ids:
        entry = entries.get(cid)
        if not entry:
            flags.append("UNKNOWN_CLAIM:" + str(cid))
            continue
        if entry["approval_status"] != "approved":
            flags.append("CLAIM_NOT_APPROVED:" + cid)
        if entry.get("expiry_or_review_date") and entry["expiry_or_review_date"] < now()[:10]:
            flags.append("CLAIM_REVIEW_EXPIRED:" + cid)
        if norm(entry.get("email_phrase") or entry["text"]).lower() not in norm(text).lower():
            flags.append("CLAIM_SCOPE_OR_TEXT_CHANGED:" + cid)
    if not set(ids).issubset(e.get("selected_claim_ids", [])):
        flags.append("CLAIMS_OUTSIDE_EVIDENCE_SELECTION")
    # Catch undeclared registry facts even if the model omitted them from its IDs.
    for cid, entry in entries.items():
        if norm(entry["text"]) in norm(text + " " + subject) and cid not in ids:
            flags.append("UNDECLARED_CLAIM:" + cid)
    greeting = "Hi " + pub["confirmed_first_name"] + "," if pub.get("confirmed_first_name") and pub.get("name_source") else "Hello,"
    if not text.startswith(greeting):
        flags.append("RECIPIENT_GREETING_MISMATCH")
    sender = draft.get("sender", {})
    if not sender.get("name") or not sender.get("email") or sender.get("company") != "Nace AI":
        flags.append("SENDER_INCOMPLETE")
    if f"I'm {sender.get('name')} from Nace AI" not in text.replace("’", "'") or not text.rstrip().endswith(f"Best,\n{sender.get('name')}"):
        flags.append("SENDER_IDENTITY_MISMATCH")
    article_url = e.get("article_url") or ""
    opening = text.split("\n\n")[1] if len(text.split("\n\n")) > 1 else ""
    if not article_url or article_url not in opening:
        flags.append("ARTICLE_LINK_MISSING_FROM_OPENING")
    urls = [u.rstrip('.,;') for u in re.findall(r"https?://[^\s<>\"()—]+", text)]
    links = [entries[i]["text"] for i in ids if i in entries and entries[i]["scope"] == "link"]
    if len(links) != 1 or urls.count(links[0]) != 1 or any(u not in links + [article_url] for u in urls):
        flags.append("PRIMARY_LINK_OR_UNSUPPORTED_URL")
    if e.get("suggested_ask") not in text:
        flags.append("ASK_CHANGED_OR_MISSING")
    if re.search(r"\b(released|launched|available now|now available)\b", text + " " + subject, re.I) and (facts["launch_status"] != "released_confirmed" or "launch.schedule" not in ids):
        flags.append("LAUNCH_STATUS_UNCONFIRMED")
    if re.search(r"\b(guarantee\w*|unlimited|pay you|sponsor\w*|research.team meeting|followed your work for years)\b|\$\d|\[[A-Z_ ]{3,}\]|\{\{", text + " " + subject, re.I):
        flags.append("UNSUPPORTED_PROMISE_OR_PLACEHOLDER")
    # Numeric product prose is allowed only inside selected verbatim scoped claims.
    residue = text + " " + subject
    for cid in ids:
        if cid in entries:
            residue = residue.replace(entries[cid]["text"], "")
            residue = residue.replace(entries[cid].get("email_phrase") or "\x00", "")
    residue = residue.replace(article_url, "").replace(e.get("specific_point") or "\x00", "")
    residue = residue.replace(sender.get("email") or "\x00", "")
    if re.search(r"\d+\s*(?:%|ms|benchmarks?|credits?|questions?)", residue, re.I):
        flags.append("UNSUPPORTED_NUMERIC_CLAIM")
    if not pub.get("contact_route") or not pub.get("contact_source_url") or not pub.get("contact_confirmed"):
        flags.append("CONTACT_UNVERIFIED")
    if pub.get("contact_kind") not in ("business", "contact", "submission", "user_supplied"):
        flags.append("EDITORIAL_CONTACT_ROUTE_REQUIRED")
    if store.suppressed(pub):
        flags.append("DO_NOT_CONTACT")
    if store.history(pub):
        flags.append("PREVIOUS_CONTACT_REVIEW_REQUIRED")
    for r in store.db.execute("SELECT id FROM publications WHERE id<>? AND status='Approved'", (pub["id"],)):
        other = store.get(r[0])
        if other["author_key"] == pub["author_key"] or (store.contact_key(pub) and store.contact_key(pub) == store.contact_key(other)):
            flags.append("AUTHOR_ALREADY_HAS_APPROVED_DRAFT")
    for r in store.db.execute("SELECT data FROM drafts WHERE publication_id<>? ORDER BY id DESC LIMIT 500", (pub["id"],)):
        previous = json.loads(r[0]).get("plain_text_body", "").split("\n\n")
        if len(previous) > 1 and SequenceMatcher(None, re.sub(r"https?://\S+", "", opening).lower(), re.sub(r"https?://\S+", "", previous[1]).lower()).ratio() > .82:
            flags.append("REPETITIVE_OPENING")
            break
    flags += pub.get("import_flags", [])
    flags.append("HUMAN_SEMANTIC_REVIEW_REQUIRED")
    flags = sorted(set(flags))
    return {"review_flags": flags, "blocking_flags": [f for f in flags if f not in WARNINGS],
            "method": "deterministic", "semantic_checks": "Human review required; automated checks are not a semantic guarantee.",
            "ready_for_review": not any(f in flags for f in ("NEEDS_RESEARCH", "EXCERPT_NOT_IN_SOURCE", "ARTICLE_NOT_COLLECTED", "FIT_NOT_SUPPORTED")),
            "body_word_count": style["body_word_count"], "style_version": draft.get("style_version", 2), "checked_at": now()}


def body_paragraphs(text):
    parts = text.strip().split("\n\n")
    # Greeting, three body paragraphs, developer link, signature. The link and signature
    # are excluded from the requested body-only word count. URLs count as one token.
    return parts[1:-2] if len(parts) >= 4 else []


def style_checks(text, subject, evidence, sender):
    paras = body_paragraphs(text)
    count = len(" ".join(paras).split())
    flags = []
    if len(paras) != 3:
        flags.append("THREE_BODY_PARAGRAPHS_REQUIRED")
    if count > 110:
        flags.append("BODY_EXCEEDS_110_WORD_HARD_MAX")
    elif not 60 <= count <= 90:
        flags.append("BODY_OUTSIDE_60_90_WORD_TARGET")
    if len(re.findall(r"\?", " ".join(paras))) != 1:
        flags.append("ONE_PRIMARY_ASK_REQUIRED")
    if not paras or evidence.get("article_url", "\x00") not in paras[0]:
        flags.append("ARTICLE_LINK_MISSING_FROM_OPENING")
    parts = text.strip().split("\n\n")
    if len(parts) < 2 or parts[-2] != "Explore Drex: https://nace.ai/developers":
        flags.append("DEVELOPER_LINK_REQUIRED_AFTER_BODY")
    if re.search(r"\{\{?|\}\}?|\[(?:name|author|recipient|insert|link|url|tbd|todo)[^\]]*\]|\b(?:TBD|TODO|PLACEHOLDER|INSERT HERE)\b|<\s*(?:name|author|recipient|link)\s*>", text + " " + subject, re.I):
        flags.append("UNRESOLVED_PLACEHOLDER")
    if re.search(r"(?:review_flags|supporting_excerpt|NEEDS_RESEARCH|evidence_record|MOCK PREVIEW|permission_basis)", text, re.I):
        flags.append("INTERNAL_NOTES_IN_EMAIL")
    detail = evidence.get("email_detail")
    if not detail:
        flags.append('ARTICLE_DETAIL_MISSING')
    elif norm(detail).lower() not in norm(evidence.get("supporting_excerpt")).lower() or norm(detail).lower() not in norm(paras[0] if paras else "").lower():
        flags.append("ARTICLE_DETAIL_NOT_SUPPORTED")
    return {"body_word_count": count, "flags": flags}
