from __future__ import annotations

import csv
import io
import json
from .common import FITS, digest, dumps, html_email, now, publication_url, safe_url
from .evidence import evidence_checks, prepare_evidence, registry, validate
from .models import MockWriter, OpenAIWriter, writing_input
from .sources import Collector, Run, save_articles
from .store import Store
from .workbook import find_workbook


class Pipeline:
    def __init__(self, config):
        self.config = config
        self.store = Store(config["database"])

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
            items.append({k: p.get(k) for k in ("id", "publication", "author_name", "status", "fit_category", "fit_reasons", "owner", "score", "publication_url", "profile_url", "author_publications", "do_not_contact", "source_kind")})
        return {"items": items, "total": total, "offset": offset}

    def pilot(self, input_path=None, batch=None, shortlist=None):
        result = self.store.import_workbook(find_workbook(input_path or self.config.get("input_path")))
        result.update(self.store.shortlist(shortlist or self.config["shortlist_size"], batch or self.config["batch_size"]))
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
            writer = MockWriter() if self.config["provider"] == "mock" else OpenAIWriter(self.store, self.config, run)
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

    def export(self, fmt="json", scope="pilot"):
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
            raise ValueError("Export format must be csv or json")
        fields = ["publication", "author", "publication_url", "article_url", "supporting_excerpt", "angle", "contact", "contact_source", "subject", "email_body", "owner", "status", "review_flags"]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: csv_safe(dumps(v) if isinstance(v, list) else v) for k, v in row.items()})
        return output.getvalue()


def csv_safe(value):
    text = str(value or "")
    # Quoting alone does not prevent spreadsheet formula evaluation.
    return "'" + text if text.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text
