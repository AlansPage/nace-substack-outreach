from __future__ import annotations

import csv
import html
import io
import json
from pathlib import Path
from urllib.parse import quote, urlsplit

from .common import FITS, digest, dumps, html_email, now, publication_url, safe_url
from .config import ROOT
from .evidence import evidence_checks, prepare_evidence, registry, validate
from .models import LocalWriter, MockWriter, OpenAIWriter, writing_input
from .sources import Collector, Run, save_articles
from .store import Store, load_dnc
from .workbook import find_workbook


CONTACT_CHANNELS = ["Email", "X", "LinkedIn", "Substack DM", "Other"]
UPLOAD_MAX_ROWS = 5000


class Pipeline:
    def __init__(self, config, actor=None):
        self.config = config
        self.store = Store(config["database"], actor)

    def facts(self):
        return registry(self.store, self.config["registry_path"])

    def detail(self, pid):
        facts, h = self.facts()
        pub = self.store.get(pid)
        if pub["draft"]:
            draft = pub["draft"]
            draft["current_sender"] = self.config["sender"]
            draft["validation"] = validate(self.store, pub, draft, facts, h)
            draft["review_flags"] = draft["validation"]["review_flags"]
            if pub["status"] in ("Approved", "Reviewed") and draft["approved_hash"] and (draft["validation"]["blocking_flags"] or draft["approved_hash"] != draft["hash"]):
                self.store.invalidate(pid, "Current validation no longer permits approval")
                self.store.db.commit()
                pub["status"] = self.store.get(pid)["status"]
                draft["approved_hash"] = None
        return pub

    def queue(self, scope="pilot", search="", status="", limit=100, offset=0):
        self.facts()
        where, args = [], []
        if scope in ("pilot", "shortlisted"):
            where.append(scope + "=1")
        if status:
            where.append("status=?")
            args.append(status)
        if search:
            where.append("data LIKE ?")
            args.append("%" + search + "%")
        clause = " WHERE " + " AND ".join(where) if where else ""
        total = self.store.db.execute("SELECT count(*) FROM publications" + clause, args).fetchone()[0]
        rows = self.store.db.execute("SELECT id FROM publications" + clause + " ORDER BY score DESC,id LIMIT ? OFFSET ?", (*args, min(200, max(1, limit)), max(0, offset))).fetchall()
        items = []
        for r in rows:
            p = self.store.get(r[0])
            item = {k: p.get(k) for k in ("id", "publication", "author_name", "status", "fit_category", "fit_reasons", "owner", "score", "publication_url", "profile_url", "author_publications", "do_not_contact", "source_kind")}
            # Latest contact with this writer on any channel, shared across their publications.
            item["contacted"] = {k: p["previous_contact"][0].get(k) for k in ("channel", "event", "at", "actor")} if p["previous_contact"] else None
            items.append(item)
        return {"items": items, "total": total, "offset": offset}

    def dnc_list(self):
        path = Path(self.config["dnc_path"])
        if not path.is_file():
            path = ROOT / "substack_outreach/dnc.example.json"
        return load_dnc(path), path.name

    def import_workbook(self, input_path=None, batch=None, shortlist=None):
        result = self.store.import_workbook(find_workbook(input_path or self.config.get("input_path")))
        return self.after_import(result, batch, shortlist)

    def upload_rows(self, data):
        """One batch of an uploaded table (the browser splits big files). The last batch applies DNC and the shortlist."""
        sheet, headers, rows = data.get("sheet"), data.get("headers"), data.get("rows")
        if not isinstance(sheet, str) or not isinstance(headers, list) or not all(isinstance(h, str) and h for h in headers):
            raise ValueError("Upload needs a sheet name and a list of column headers")
        if len(set(headers)) != len(headers) or not {"url", "pub_id"} & set(headers):
            raise ValueError(f"Sheet {sheet!r} is not a Substack publication table (needs url or pub_id columns)")
        if not isinstance(rows, list) or len(rows) > UPLOAD_MAX_ROWS or not all(isinstance(r, dict) for r in rows):
            raise ValueError(f"Upload rows as a list of objects, at most {UPLOAD_MAX_ROWS} per request")
        offset = int(data.get("offset") or 0)
        cell = lambda v: None if v is None or v == "" else (v if isinstance(v, str) else str(v))
        table = [(int(r["_row"]) if str(r.get("_row", "")).isdigit() else offset + n + 2, {h: cell(r.get(h)) for h in headers})
                 for n, r in enumerate(rows)]
        label = f"upload:{sheet} by {self.store.actor or 'local operator'}"
        result = self.store.import_rows(headers, table, digest(["upload", sheet, headers, table]), label)
        result["sheet"] = sheet
        return self.after_import(result) if data.get("last") is True else result

    def after_import(self, result, batch=None, shortlist=None):
        entries, name = self.dnc_list()
        result["dnc_list"] = name
        result["dnc_suppressed"] = self.store.apply_dnc(entries, name)
        result.update(self.store.shortlist(shortlist or self.config["shortlist_size"], batch or self.config["batch_size"]))
        return result

    def pilot(self, input_path=None, batch=None, shortlist=None):
        result = self.import_workbook(input_path, batch, shortlist)
        result["processing"] = self.process(batch or self.config["batch_size"])
        return result

    def process(self, batch=None, collect=False):
        run = Run(self.store, self.config, "pilot")
        collector = Collector(self.store, self.config, run)
        results = []
        try:
            ids = self.store.db.execute("SELECT id FROM publications WHERE pilot=1 AND status NOT IN ('Skipped','Do not contact','Sent','Replied','Approved','Reviewed') ORDER BY score DESC,id LIMIT ?", (batch or self.config["batch_size"],)).fetchall()
            for row in ids:
                pub = self.store.get(row[0])
                if pub["draft"]:
                    results.append({"id": pub["id"], "result": "existing_draft_preserved"})
                    continue
                if self.store.suppressed(pub) or self.store.history(pub):
                    results.append({"id": pub["id"], "result": "contact_history_or_suppression"})
                    continue
                stage = 'research'
                try:
                    if not pub["articles"] and collect:
                        save_articles(self.store, pub, collector.collect(pub))
                        pub = self.store.get(pub["id"])
                    e = prepare_evidence(pub)
                    pub["evidence"] = e
                    pub["status"] = "Researched" if e.get("ready_to_draft") else "Needs research"
                    self.store.save(pub)
                    self.store.db.commit()
                    if e.get("ready_to_draft"):
                        stage = 'generation'
                        self.generate(pub["id"], run=run)
                        results.append({"id": pub["id"], "result": "drafted"})
                    else:
                        results.append({"id": pub["id"], "result": e["result"]})
                except (ValueError, OSError) as exc:
                    pub = self.store.get(pub["id"])
                    pub["status"] = "Researched" if stage == 'generation' else "Needs research"
                    pub["draft_error" if stage == 'generation' else "research_error"] = str(exc)
                    self.store.save(pub)
                    self.store.event(pub["id"], "generation_failed" if stage == 'generation' else "research_failed", str(exc))
                    self.store.db.commit()
                    results.append({"id": pub["id"], "error": str(exc)})
                    if 'Per-run' in str(exc):
                        break
            run.finish()
        except Exception:
            run.finish("Run interrupted; committed work is resumable")
            raise
        return results

    def collect_contacts(self, batch=None, retry_failed=False):
        if not self.config.get("collect_contacts"):
            raise ValueError("CONTACT_COLLECTION_DISABLED: set collect_contacts: true in the local config")
        if not self.config["network_enabled"]:
            raise ValueError("NETWORK_DISABLED: enable network access to read publication about pages")
        run = Run(self.store, self.config, "contacts")
        collector = Collector(self.store, self.config, run)
        results = []
        try:
            rows = self.store.db.execute("SELECT id,data FROM publications WHERE pilot=1 AND status NOT IN ('Skipped','Do not contact','Sent','Replied') ORDER BY score DESC,id").fetchall()
            for row in rows:
                if len(results) >= (batch or self.config["batch_size"]):
                    break
                data = json.loads(row["data"])
                previous = (data.get("contact_route_proposed") or {}).get("status")
                if not publication_url(data.get("publication_url")) or data.get("contact_confirmed") or (previous and not (retry_failed and previous == "failed")):
                    continue
                pub = self.store.get(row["id"])
                try:
                    about, candidates = collector.about_contacts(pub)
                except ValueError as exc:
                    if str(exc).startswith("Per-run"):
                        results.append({"id": pub["id"], "publication": pub["publication"], "error": str(exc)})
                        break
                    # Robots refusals, HTTP barriers and oversize pages are remembered; --retry-failed tries again.
                    pub["contact_route_proposed"] = {"route": "", "candidates": [], "contact_source_url": pub["publication_url"] + "/about",
                                                     "contact_confirmed": False, "status": "failed", "error": str(exc), "collected_at": now()}
                    with self.store.db:
                        self.store.save(pub)
                        self.store.event(pub["id"], "contact_collection_failed", str(exc))
                    results.append({"id": pub["id"], "publication": pub["publication"], "error": str(exc)})
                    continue
                # A proposal only. contact_route is written solely by a human confirming it in the UI.
                pub["contact_route_proposed"] = {"route": candidates[0]["route"] if candidates else "", "candidates": candidates,
                                                 "contact_source_url": about, "contact_confirmed": False,
                                                 "status": "proposed" if candidates else "none_found", "collected_at": now()}
                with self.store.db:
                    self.store.save(pub)
                    self.store.event(pub["id"], "contact_proposed", {"source": about, "candidates": len(candidates)})
                results.append({"id": pub["id"], "publication": pub["publication"], "source": about,
                                "result": pub["contact_route_proposed"]["status"], "candidates": candidates})
            run.finish()
        except Exception:
            run.finish("Run interrupted; committed work is resumable")
            raise
        return results

    def resolve_contact_proposal(self, pid, data):
        pub = self.store.get(pid)
        proposal = pub.get("contact_route_proposed") or {}
        if proposal.get("status") != "proposed":
            raise ValueError("No unresolved contact proposal for this publication")
        if not str(data.get("reviewer") or "").strip():
            raise ValueError("A named reviewer is required to resolve a contact proposal")
        decision = data.get("decision")
        if decision == "confirm":
            route = data.get("route") or proposal["route"]
            if route not in [c["route"] for c in proposal["candidates"]]:
                raise ValueError("Confirm one of the proposed routes; enter any other route manually with its own source")
            # Reuse the ordinary recipient validation; the about page is the provenance.
            self.update(pid, {"contact_route": route, "contact_source_url": proposal["contact_source_url"],
                              "contact_kind": data.get("contact_kind") or "contact", "contact_confirmed": True})
            pub = self.store.get(pid)
        elif decision != "dismiss":
            raise ValueError("decision must be confirm or dismiss")
        pub["contact_route_proposed"] = dict(proposal, status="confirmed" if decision == "confirm" else "dismissed",
                                             contact_confirmed=decision == "confirm", decided_by=data["reviewer"].strip(), decided_at=now())
        with self.store.db:
            self.store.save(pub)
            self.store.event(pid, "contact_proposal_" + ("confirmed" if decision == "confirm" else "dismissed"), {"reviewer": data["reviewer"].strip()})
        return self.detail(pid)

    def match_publication(self, url):
        """Publication id for an exported newsletter URL: canonical publication_url, then host."""
        target = publication_url(url)
        if not target:
            return None, "no_publication_url"
        exact = self.store.db.execute("SELECT id FROM publications WHERE json_extract(data,'$.publication_url')=?", (target,)).fetchall()
        if len(exact) == 1:
            return exact[0][0], "publication_url"
        host = urlsplit(target).hostname
        same_host = [r[0] for r in self.store.db.execute("SELECT id,json_extract(data,'$.publication_url') FROM publications")
                     if r[1] and (urlsplit(r[1]).hostname or "").removeprefix("www.") == host]
        if len(exact) > 1 or len(same_host) > 1:
            return None, "ambiguous"
        return (same_host[0], "host") if same_host else (None, "no_match")

    def import_reply_to(self, records):
        """Gmail reply-to export -> unconfirmed contact routes. Unmatched records go to the report only."""
        if not isinstance(records, list):
            raise ValueError("Reply-to import expects a JSON array")
        report = {"imported": [], "unmatched": [], "skipped": [], "invalid": []}
        seen = set()
        for n, r in enumerate(records):
            item = {"index": n, **{k: (r.get(k) if isinstance(r, dict) else None) for k in ("publication_url", "sender_name", "reply_to", "message_url")}}
            if not isinstance(r, dict) or not item["reply_to"] or not str(item["message_url"] or "").startswith("https://mail.google.com/"):
                report["invalid"].append(item | {"reason": "reply_to and a Gmail message_url are required"})
                continue
            pid, how = self.match_publication(item["publication_url"])
            if pid is None:
                report["unmatched"].append(item | {"reason": how})
                continue
            pub = self.store.get(pid)
            if pid in seen or pub.get("contact_route"):
                # Never replace a route already recorded (or imported earlier in this file).
                report["skipped"].append(item | {"publication_id": pid, "reason": "publication already has a contact route"})
                continue
            try:
                self.update(pid, {"contact_route": str(item["reply_to"]).strip(), "contact_source_url": item["message_url"],
                                  "contact_kind": "reply_to", "contact_confirmed": False})
            except ValueError as exc:
                report["invalid"].append(item | {"publication_id": pid, "reason": str(exc)})
                continue
            seen.add(pid)
            self.store.event(pid, "reply_to_imported", {"source": item["message_url"], "matched_by": how})
            self.store.db.commit()
            report["imported"].append(item | {"publication_id": pid, "publication": pub["publication"], "matched_by": how})
        return report

    def save_draft(self, pub, draft, facts, h):
        draft["html_body"] = html_email(draft["plain_text_body"])
        draft["registry_hash"] = h
        draft["validation"] = validate(self.store, pub, draft, facts, h)
        draft["review_flags"] = draft["validation"]["review_flags"]
        payload_hash = digest({k: draft[k] for k in ("subject", "plain_text_body", "html_body", "evidence_record", "claim_ids_used", "sender", "registry_hash", "provider", "style_version")})
        version = self.store.db.execute("SELECT coalesce(max(version),0)+1 FROM drafts WHERE publication_id=?", (pub["id"],)).fetchone()[0]
        with self.store.db:
            self.store.invalidate(pub["id"], "Draft edited or regenerated")
            self.store.db.execute("INSERT INTO drafts(publication_id,version,data,hash,created_at) VALUES(?,?,?,?,?)", (pub["id"], version, dumps(draft), payload_hash, now()))
            pub["status"] = "Drafted"
            self.store.save(pub)
            self.store.event(pub["id"], "draft_saved", {"version": version, "hash": payload_hash, "provider": draft["provider"]})
        return self.detail(pub["id"])

    def generate(self, pid, regenerate=False, run=None):
        pub = self.store.get(pid)
        if pub["draft"] and not regenerate:
            return self.detail(pid)
        if pub["status"] in ("Sent", "Replied", "Do not contact", "Skipped") or self.store.suppressed(pub) or self.store.history(pub):
            raise ValueError("Generation blocked by status, prior contact or do-not-contact flag")
        if regenerate:
            self.store.invalidate(pid, "Explicit regeneration requested")
            self.store.db.commit()
            pub = self.store.get(pid)
        evidence = prepare_evidence(pub)
        blocking = [f for f in evidence_checks(pub, evidence) if f in ("NEEDS_RESEARCH", "EXCERPT_NOT_IN_SOURCE", "ARTICLE_NOT_COLLECTED", "FIT_NOT_SUPPORTED", "INCOMPLETE_ANGLE_OR_ASK")]
        if blocking:
            raise ValueError("NEEDS_RESEARCH: " + ", ".join(blocking))
        facts, h = self.facts()
        pub["evidence"] = evidence
        self.store.save(pub)
        self.store.db.commit()
        own_run = run is None
        run = run or Run(self.store, self.config, "regenerate" if regenerate else "generate")
        try:
            provider = self.config["provider"]
            writer = MockWriter(facts) if provider == "mock" else LocalWriter(self.store, self.config, run) if provider == "local" else OpenAIWriter(self.store, self.config, run)
            payload = writing_input(pub, evidence, facts, self.config["sender"])
            if writer.name != "mock" and not evidence.get("evidence_reviewed_by"):
                raise ValueError("Review the evidence-backed angle before live writing")
            draft, attempts = None, []
            for attempt in range(2):
                previous = {"draft": draft["plain_text_body"], "failed_checks": attempts[-1]} if draft else None
                output = writer.generate(payload, evidence, revision=previous)
                current_evidence = dict(evidence, email_detail=output["email_detail"])
                draft = {"subject": output["subject"], "plain_text_body": output["plain_text_body"],
                         "claim_ids_used": output["claim_ids_used"], "evidence_record": current_evidence,
                         "sender": self.config["sender"], "registry_hash": h, "provider": writer.name,
                         "style_version": 2, "model": self.config.get("model") if writer.name != "mock" else None}
                checks = validate(self.store, pub, draft, facts, h)
                # Source/approval warnings cannot be repaired by the writer. Only revise email defects.
                repairable = [f for f in checks["blocking_flags"] if not f.startswith(("CLAIM_NOT_APPROVED:", "CONTACT_", "EDITORIAL_CONTACT", "MOCK_", "FIXTURE_", "HUMAN_", "ARTICLE_MEMBERSHIP"))]
                attempts.append(repairable)
                if not repairable:
                    break
            draft["generation_attempts"] = attempts
            result = self.save_draft(pub, draft, facts, h)
            if own_run:
                run.finish()
            return result
        except Exception:
            if own_run:
                run.finish("Generation failed; see caller error. No credentials logged.")
            raise

    def edit(self, pid, data):
        pub = self.store.get(pid)
        if pub["status"] in ("Sent", "Replied"):
            raise ValueError("Sent/replied draft snapshots are immutable; record history separately")
        if not pub["draft"]:
            raise ValueError("Generate a draft or add research first")
        facts, h = self.facts()
        draft = {k: v for k, v in pub["draft"].items() if k not in ("hash", "version", "approved_hash", "approved_by", "approved_at", "current_sender")}
        draft["subject"] = str(data["subject"])
        draft["plain_text_body"] = str(data["plain_text_body"])
        draft["claim_ids_used"] = data.get("claim_ids_used", draft["claim_ids_used"])
        prior_detail = draft['evidence_record'].get('email_detail')
        draft["evidence_record"] = dict(pub["evidence"] or draft["evidence_record"])
        draft['evidence_record'].setdefault('email_detail', prior_detail)
        draft["sender"] = self.config["sender"]
        if data.get("human_authored"):
            draft["provider"] = "human"
        # Saving never applies a template or changes the user's text.
        return self.save_draft(pub, draft, facts, h)

    def update(self, pid, data):
        pub = self.store.get(pid)
        allowed = {"owner", "author_name", "confirmed_first_name", "name_source", "contact_route", "contact_source_url", "contact_kind", "contact_confirmed", "research", "fit_category", "fit_reasons", "evidence", "publication_url", "publication_url_source"}
        if data.get("fit_category", pub["fit_category"]) not in FITS:
            raise ValueError("Invalid fit category")
        if "publication_url" in data and data["publication_url"] != pub.get("publication_url"):
            if not publication_url(data["publication_url"]) or not data.get("publication_url_source"):
                raise ValueError("Resolved publication URL requires supporting evidence; profile URLs stay separate")
            data["publication_url"] = publication_url(data["publication_url"])
        first = data.get("confirmed_first_name", pub.get("confirmed_first_name"))
        name_source = data.get("name_source", pub.get("name_source"))
        if first and (not name_source or "@" in first or len(first.split()) > 2):
            raise ValueError("Confirmed public first name requires a source; never derive it from a handle")
        route = data.get("contact_route", pub.get("contact_route"))
        if route and not (safe_url(route) or ("@" in route and " " not in route and "\n" not in route)):
            raise ValueError("Contact route must be an explicit email or HTTP(S) contact/submission URL")
        changed = any(pub.get(k) != v for k, v in data.items() if k in allowed and k != "owner")
        if changed and pub["status"] in ("Sent", "Replied"):
            raise ValueError("Sent/replied evidence is immutable")
        with self.store.db:
            if changed:
                self.store.invalidate(pid, "Recipient, evidence or research settings changed")
                if pub["status"] in ("Approved", "Reviewed"):
                    pub["status"] = "Drafted"
            pub.update({k: v for k, v in data.items() if k in allowed})
            if "evidence" not in data and changed:
                pub["evidence"] = None
            if route:
                self.store.link_identity(pub, "contact:" + self.store.contact_key(pub))
            self.store.save(pub)
        return self.detail(pid)

    def action(self, pid, action, data):
        if action == "regenerate":
            return self.generate(pid, regenerate=True)
        if action == "edit":
            return self.edit(pid, data)
        if action == "update":
            return self.update(pid, data)
        if action == "research":
            pub = self.store.get(pid)
            save_articles(self.store, pub, data["articles"], supplied=True)
            return self.detail(pid)
        if action == "evidence":
            return self.update(pid, {"evidence": data})
        if action == "contact_proposal":
            return self.resolve_contact_proposal(pid, data)
        pub = self.detail(pid)
        with self.store.db:
            if action in ("approve", "review"):
                if not pub["draft"] or not data.get("reviewer") or not data.get("semantic_review_confirmed"):
                    raise ValueError("Reviewer and explicit semantic review confirmation are required")
                if pub["draft"]["validation"]["blocking_flags"]:
                    raise ValueError("Resolve blocking flags: " + ", ".join(pub["draft"]["validation"]["blocking_flags"]))
                pub["status"] = "Approved" if action == "approve" else "Reviewed"
                if action == "approve":
                    self.store.db.execute("UPDATE drafts SET approved_hash=hash,approved_by=?,approved_at=? WHERE publication_id=? AND version=?", (data["reviewer"], now(), pid, pub["draft"]["version"]))
                self.store.event(pid, action, {"reviewer": data["reviewer"], "hash": pub["draft"]["hash"], "version": pub["draft"]["version"], "semantic_review": "human"})
            elif action == "skip":
                if not data.get("reason"):
                    raise ValueError("A skip reason is required")
                self.store.invalidate(pid, "Skipped")
                pub.update(status="Skipped", skip_reason=data["reason"])
            elif action == "dnc":
                self.store.suppress(pub, data.get("reason") or "User requested do not contact")
                pub["status"] = "Do not contact"
            elif action == "sent":
                if pub["status"] != "Approved" or not pub["draft"]["approved_hash"]:
                    raise ValueError("Only the current approved draft can be manually recorded as sent")
                self.store.record_contact(pub, "email", "Sent", data.get("source") or "Manual record in local review UI", data.get("note", ""))
                pub["status"] = "Sent"
            elif action == "replied":
                if pub["status"] != "Sent":
                    raise ValueError("Record a sent message before a reply")
                self.store.record_contact(pub, "email", "Replied", data.get("source") or "Manual record in local review UI", data.get("note", ""))
                pub["status"] = "Replied"
            elif action == "contacted":
                # The quick "Contacted" checkbox: any channel, who and when, so teammates never ping a writer twice.
                if data.get("confirmed") is not True:
                    raise ValueError("Tick Contacted to confirm this writer was reached")
                if data.get("channel") not in CONTACT_CHANNELS:
                    raise ValueError("Channel must be one of: " + ", ".join(CONTACT_CHANNELS))
                link = str(data.get("link") or "").strip()
                if link and not safe_url(link).startswith("https://"):
                    raise ValueError("Link must be an https URL to the message or profile")
                source = link or "Marked contacted in the workspace by " + (self.store.actor or "the local operator")
                self.store.record_contact(pub, data["channel"], "Contacted", source, str(data.get("note") or ""),
                                          dedup=digest([pub["author_key"], data["channel"], source, now()[:10]]))
                self.store.invalidate(pid, "Writer marked contacted")
                if pub["status"] in ("Approved", "Reviewed"):
                    pub["status"] = "Drafted"
            elif action == "history":
                if data.get("identity_alias"):
                    self.store.link_identity(pub, data["identity_alias"])
                self.store.record_contact(pub, data["channel"], data.get("event") or "Previously contacted", data["source"], data.get("note", ""), data.get("at"), digest(data))
                self.store.invalidate(pid, "Shared contact history changed")
                if pub["status"] in ("Approved", "Reviewed"):
                    pub["status"] = "Drafted"
            else:
                raise ValueError("Unknown action")
            self.store.save(pub)
        return self.detail(pid)

    def sendsheet(self, scope="pilot"):
        rows = []
        clause = " AND pilot=1" if scope == "pilot" else " AND shortlisted=1" if scope == "shortlisted" else ""
        for r in self.store.db.execute("SELECT id FROM publications WHERE status='Approved'" + clause + " ORDER BY score DESC,id").fetchall():
            # detail() re-validates and withdraws an approval that no longer holds.
            p = self.detail(r[0])
            d = p.get("draft") or {}
            if p["status"] != "Approved" or not d.get("approved_hash") or d["approved_hash"] != d["hash"]:
                continue
            rows.append({"publication": p["publication"], "recipient": p["contact_route"], "contact_source": p["contact_source_url"],
                         "subject": d["subject"], "body": d["plain_text_body"], "approved_by": d["approved_by"],
                         "approved_at": d["approved_at"], "version": d["version"], "hash": d["hash"]})
        return sendsheet_html(rows, now())

    def export(self, fmt="json", scope="pilot"):
        if fmt == "sendsheet":
            return self.sendsheet(scope)
        rows = []
        clause = " WHERE pilot=1" if scope == "pilot" else " WHERE shortlisted=1" if scope == "shortlisted" else ""
        for r in self.store.db.execute("SELECT id FROM publications" + clause + " ORDER BY score DESC,id").fetchall():
            p = self.detail(r[0])
            draft, e = p.get("draft") or {}, p.get("evidence") or {}
            rows.append({"publication": p["publication"], "author": p["author_name"], "publication_url": p["publication_url"],
                         "article_url": e.get("article_url", ""), "supporting_excerpt": e.get("supporting_excerpt", ""),
                         "angle": e.get("drex_connection", ""), "contact": p["contact_route"], "contact_source": p["contact_source_url"],
                         "subject": draft.get("subject", ""), "email_body": draft.get("plain_text_body", ""),
                         "owner": p["owner"], "status": p["status"], "review_flags": draft.get("review_flags", e.get("review_flags", ["NEEDS_RESEARCH"]))})
        if fmt == "json":
            return json.dumps(rows, ensure_ascii=False, indent=2)
        if fmt != "csv":
            raise ValueError("Export format must be csv, json or sendsheet")
        fields = ["publication", "author", "publication_url", "article_url", "supporting_excerpt", "angle", "contact", "contact_source", "subject", "email_body", "owner", "status", "review_flags"]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: csv_safe(dumps(v) if isinstance(v, list) else v) for k, v in row.items()})
        return output.getvalue()


def mailto_link(to, subject, body):
    # RFC 6068: percent-encode everything, CRLF line breaks. Opening it only fills a compose window.
    crlf = lambda text: text.replace("\r\n", "\n").replace("\n", "\r\n")
    return "mailto:" + quote(to, safe="@") + "?subject=" + quote(crlf(subject), safe="") + "&body=" + quote(crlf(body), safe="")


SENDSHEET_STYLE = ("*{box-sizing:border-box}body{font:15px/1.6 system-ui,sans-serif;max-width:860px;margin:40px auto;padding:0 20px;color:#1d3029}"
                   "header p{color:#5d6d64}article{border-top:1px solid #cbd7d0;padding:22px 0}h2{font-size:19px;margin:0 0 6px}"
                   ".meta{font-size:12px;color:#5d6d64}dt{font-weight:600;font-size:12px;margin-top:12px}dd{margin:4px 0}"
                   "textarea{width:100%;min-height:230px;font:14px/1.6 ui-monospace,monospace;padding:10px}"
                   "button,.mailto{font:inherit;font-size:13px;margin:6px 8px 0 0;padding:7px 12px;border:1px solid #9fb4a9;"
                   "border-radius:6px;background:#fff;color:#183b31;text-decoration:none;display:inline-block;cursor:pointer}"
                   ".note{background:#fff6e4;color:#7a5418;padding:10px;font-size:13px}")
SENDSHEET_SCRIPT = ("document.addEventListener('click',function(e){var b=e.target.closest('button[data-copy]');if(!b)return;"
                    "var t=document.getElementById(b.dataset.copy);var done=function(){b.textContent='Copied';"
                    "setTimeout(function(){b.textContent=b.dataset.label},1500)};"
                    "if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t.value).then(done,function(){t.select();document.execCommand('copy');done()})}"
                    "else{t.select();document.execCommand('copy');done()}});")


def sendsheet_html(rows, generated_at):
    esc = lambda value: html.escape(str(value or ""), quote=True)
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
           '<meta name="robots" content="noindex, nofollow"><title>Approved drafts - manual send sheet</title><style>' + SENDSHEET_STYLE + '</style></head><body>'
           '<header><h1>Approved drafts</h1><p>Generated ' + esc(generated_at) + ' · ' + str(len(rows)) + ' approved publication(s).</p>'
           '<p class="note">Nothing is sent from this page. Copy or open each draft in your own mail client, send it yourself, '
           'then use <strong>Record sent manually</strong> in the workspace. This file holds contact data: keep it out of public/ and out of git.</p></header>']
    for n, r in enumerate(rows):
        to, subject, body = r["recipient"], r["subject"], r["body"]
        is_email = "@" in to and not to.lower().startswith(("http://", "https://"))
        route = to.removeprefix("mailto:")
        compose = ('<a class="mailto" href="' + esc(mailto_link(route, subject, body)) + '">Open in mail app</a>') if is_email else \
                  ('<a class="mailto" href="' + esc(safe_url(to)) + '" rel="noopener noreferrer">Open contact route</a>' if safe_url(to) else "")
        out.append('<article><h2>' + esc(r["publication"]) + '</h2><p class="meta">Approved by ' + esc(r["approved_by"]) + ' at ' + esc(r["approved_at"])
                   + ' · version ' + esc(r["version"]) + ' · ' + esc(r["hash"][:16]) + ' · contact source: ' + esc(r["contact_source"]) + '</p><dl>'
                   + ''.join('<dt>' + label + '</dt><dd><textarea id="' + key + str(n) + '" readonly' + (' rows="1" style="min-height:0"' if key != "body" else "") + '>' + esc(value) + '</textarea>'
                             + '<button type="button" data-copy="' + key + str(n) + '" data-label="Copy ' + label.lower() + '">Copy ' + label.lower() + '</button></dd>'
                             for key, label, value in (("to", "Recipient", route), ("subject", "Subject", subject), ("body", "Body", body)))
                   + '</dl>' + compose + '</article>')
    if not rows:
        out.append('<p>No publication is currently approved. Approve drafts in the workspace first.</p>')
    return "".join(out) + "<script>" + SENDSHEET_SCRIPT + "</script></body></html>"


def csv_safe(value):
    text = str(value or "")
    # Quoting alone does not prevent spreadsheet formula evaluation.
    return "'" + text if text.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text
