from __future__ import annotations

import json
from pathlib import Path

from urllib.parse import urlsplit

from .common import canonical, classify, digest, dumps, is_profile, now, publication_url
from .db import connect
from .workbook import EXPECTED, identifier, read_workbook

DNC_MATCHERS = ("hosts", "author_names", "names", "pub_ids")


def load_dnc(path):
    """Do-not-contact seed list: exact matches only, each entry with an owner, reason and source."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{path}: expected an object with an 'entries' list")
    for n, e in enumerate(entries):
        if not all(isinstance(e.get(k), str) and e[k].strip() for k in ("owner", "reason", "source")):
            raise ValueError(f"{path}: entry {n} needs owner, reason and source")
        if not any(e.get(k) for k in DNC_MATCHERS):
            raise ValueError(f"{path}: entry {n} needs at least one of {', '.join(DNC_MATCHERS)}")
    return entries


def dnc_match(entries, data):
    host = (urlsplit(data.get("publication_url") or "").hostname or "").removeprefix("www.")
    source = data.get("source") or {}
    values = {"hosts": host, "author_names": str(data.get("author_name") or "").strip().casefold(),
              "names": str(data.get("publication") or "").strip().casefold(), "pub_ids": identifier(source.get("pub_id"))}
    for e in entries:
        for key in DNC_MATCHERS:
            wanted = {identifier(v) if key == "pub_ids" else str(v).strip().casefold().removeprefix("www.") for v in e.get(key) or []}
            if values[key] and values[key] in wanted:
                return e, key
    return None, None


class Store:
    def __init__(self, path, actor=None):
        self.db = connect(path)
        # Who acted, for events written through this store (the signed-in user in hosted mode).
        self.actor = actor

    def close(self):
        self.db.close()

    def event(self, pid, kind, detail):
        self.db.execute("INSERT INTO events(publication_id,kind,detail,at,actor) VALUES(?,?,?,?,?)", (pid, kind, dumps(detail), now(), self.actor))

    def get(self, pid):
        row = self.db.execute("SELECT * FROM publications WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise ValueError("Publication not found")
        data = json.loads(row["data"])
        data.update({k: row[k] for k in ("id", "status", "score", "shortlisted", "pilot", "author_key")})
        data["articles"] = [json.loads(r["data"]) | {"id": r["id"]} for r in self.db.execute("SELECT * FROM articles WHERE publication_id=? ORDER BY id DESC", (pid,))]
        draft = self.db.execute("SELECT * FROM drafts WHERE publication_id=? ORDER BY version DESC LIMIT 1", (pid,)).fetchone()
        data["draft"] = (json.loads(draft["data"]) | {k: draft[k] for k in ("version", "hash", "approved_hash", "approved_by", "approved_at")}) if draft else None
        data["previous_contact"] = self.history(data)
        data["do_not_contact"] = self.suppressed(data)
        data["author_publications"] = self.db.execute("SELECT count(*) FROM publications WHERE author_key=?", (data["author_key"],)).fetchone()[0]
        return data

    def save(self, data):
        payload = {k: v for k, v in data.items() if k not in ("id", "status", "score", "shortlisted", "pilot", "author_key", "articles", "draft", "previous_contact", "do_not_contact", "author_publications")}
        self.db.execute("UPDATE publications SET data=?,status=?,score=?,shortlisted=?,pilot=?,author_key=? WHERE id=?",
                        (dumps(payload), data["status"], data["score"], data["shortlisted"], data["pilot"], data["author_key"], data["id"]))

    @staticmethod
    def contact_key(data):
        return (data.get("contact_route") or "").strip().lower().removeprefix("mailto:")

    def keys(self, data):
        return [data["author_key"], "contact:" + self.contact_key(data)]

    def history(self, data):
        return [dict(r) for r in self.db.execute("SELECT * FROM contacts WHERE author_key=? OR (contact_key<>'' AND contact_key=?) ORDER BY at DESC", (data["author_key"], self.contact_key(data)))]

    def suppressed(self, data):
        return bool(self.db.execute("SELECT 1 FROM suppressions WHERE key IN (?,?)", self.keys(data)).fetchone())

    def suppress(self, data, reason):
        for key in self.keys(data):
            if key != "contact:":
                self.db.execute("INSERT OR REPLACE INTO suppressions VALUES(?,?,?)", (key, reason, now()))
        for r in self.db.execute("SELECT id FROM publications WHERE author_key=?", (data["author_key"],)).fetchall():
            self.invalidate(r["id"], "Do not contact")
            self.db.execute("UPDATE publications SET status='Do not contact' WHERE id=?", (r["id"],))
        self.event(data["id"], "suppression", reason)

    def invalidate(self, pid, reason):
        self.db.execute("UPDATE drafts SET approved_hash=NULL,approved_by=NULL,approved_at=NULL WHERE publication_id=?", (pid,))
        self.db.execute("UPDATE publications SET status='Drafted' WHERE id=? AND status IN ('Approved','Reviewed')", (pid,))
        self.event(pid, "approval_invalidated", reason)

    def record_contact(self, data, channel, event, source, note="", at=None, dedup=None):
        if not channel or not source:
            raise ValueError("Contact channel and source are required")
        self.db.execute("INSERT OR IGNORE INTO contacts(author_key,contact_key,channel,event,at,source,note,dedup,actor) VALUES(?,?,?,?,?,?,?,?,?)",
                        (data["author_key"], self.contact_key(data), channel, event, at or now(), source, note, dedup, self.actor))
        self.event(data["id"], "contact_recorded", {"channel": channel, "event": event})

    def link_identity(self, data, alias):
        # Explicit user mapping only: never infer an identity from a display name/handle.
        if not alias.strip():
            return
        old = self.db.execute("SELECT author_key FROM identities WHERE alias=?", (alias,)).fetchone()
        target = old[0] if old else data["author_key"]
        current = data["author_key"]
        if target != current:
            for table in ("publications", "contacts", "identities"):
                self.db.execute(f"UPDATE {table} SET author_key=? WHERE author_key=?", (target, current))
            suppression = self.db.execute("SELECT reason,at FROM suppressions WHERE key=?", (current,)).fetchone()
            if suppression:
                self.db.execute("INSERT OR IGNORE INTO suppressions VALUES(?,?,?)", (target, *suppression))
            data["author_key"] = target
        self.db.execute("INSERT OR IGNORE INTO identities VALUES(?,?)", (alias, target))

    def import_workbook(self, path):
        fingerprint = digest(Path(path).read_bytes().hex())
        previous = self.db.execute("SELECT * FROM imports WHERE fingerprint=?", (fingerprint,)).fetchone()
        if previous:
            return {"resumed": True, "inserted": 0, "headers": json.loads(previous["headers"])}
        headers, rows = read_workbook(path)
        inserted, duplicates = 0, 0
        with self.db:
            for rowno, src in rows:
                url = publication_url(src.get("url"))
                profile = canonical(src.get("substack_profile")) or (canonical(src.get("url")) if is_profile(src.get("url")) else "")
                pub_id, author_id = identifier(src.get("pub_id")), identifier(src.get("author_id"))
                keys = (["pub:" + pub_id] if pub_id else []) + (["url:" + url] if url else [])
                keys = keys or [f"row:{fingerprint}:{rowno}"]
                found = {r[0] for key in keys for r in self.db.execute("SELECT publication_id FROM aliases WHERE key=?", (key,))}
                if len(found) > 1:
                    # Preserve both reviewed records; conflicting source identities need a human.
                    for pid in found:
                        p = self.get(pid)
                        p["import_flags"] = sorted(set(p.get("import_flags", []) + ["IDENTITY_CONFLICT"]))
                        self.save(p)
                    pid = min(found)
                elif found:
                    pid = found.pop()
                    duplicates += 1
                else:
                    fit, score, reasons = classify(src)
                    author_key = "author:" + author_id if author_id else ("profile:" + profile if profile else "unknown:" + digest(keys))
                    data = {"source": src, "publication": src.get("name"), "publication_url": url,
                            "profile_url": profile, "author_name": src.get("author_name"),
                            "fit_category": fit, "fit_reasons": reasons, "owner": "", "name_source": "",
                            "confirmed_first_name": "", "contact_route": "", "contact_source_url": "",
                            "contact_kind": "", "contact_confirmed": False, "research": {}, "evidence": None,
                            "import_flags": [], "source_kind": "workbook", "skip_reason": ""}
                    cur = self.db.execute("INSERT INTO publications(data,score,author_key) VALUES(?,?,?)", (dumps(data), score, author_key))
                    pid = cur.lastrowid
                    inserted += 1
                    for alias in (["author:" + author_id] if author_id else []) + (["profile:" + profile] if profile else []):
                        self.link_identity(self.get(pid), alias)
                for key in keys:
                    self.db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?)", (key, pid))
                self.db.execute("INSERT INTO source_rows VALUES(?,?,?,?)", (fingerprint, rowno, pid, dumps(src)))
            self.db.execute("INSERT INTO imports VALUES(?,?,?,?)", (fingerprint, str(path), dumps(headers), now()))
        return {"rows": len(rows), "inserted": inserted, "duplicates": duplicates, "headers": headers,
                "missing_headers": sorted(EXPECTED - set(headers))}

    def apply_dnc(self, entries, list_name):
        # Re-run on every import so a resumed workbook still picks up a newly added entry.
        suppressed = []
        with self.db:
            for row in self.db.execute("SELECT id,data,status FROM publications").fetchall():
                if row["status"] == "Do not contact":
                    continue
                entry, key = dnc_match(entries, json.loads(row["data"]) | {"id": row["id"]})
                if entry is None:
                    continue
                pub = self.get(row["id"])
                if self.suppressed(pub):
                    continue
                self.suppress(pub, f"DNC list ({list_name}): {entry['owner']} - {entry['reason']} [matched {key}; source: {entry['source']}]")
                suppressed.append({"id": pub["id"], "publication": pub["publication"], "owner": entry["owner"], "matched": key})
        return suppressed

    def shortlist(self, limit=100, batch=20):
        # A persisted pilot is resumable. Increasing the batch extends it; it never rotates silently.
        with self.db:
            current = self.db.execute("SELECT count(*) FROM publications WHERE shortlisted=1").fetchone()[0]
            candidates = self.db.execute("SELECT id FROM publications WHERE shortlisted=0 AND score>0 AND status='Imported' ORDER BY score DESC,id LIMIT ?", (max(0, limit-current),)).fetchall()
            for r in candidates:
                self.db.execute("UPDATE publications SET shortlisted=1,status='Shortlisted' WHERE id=?", (r[0],))
            pilot = self.db.execute("SELECT count(*) FROM publications WHERE pilot=1").fetchone()[0]
            for r in self.db.execute("SELECT id FROM publications WHERE shortlisted=1 AND pilot=0 ORDER BY score DESC,id LIMIT ?", (max(0, batch-pilot),)).fetchall():
                self.db.execute("UPDATE publications SET pilot=1 WHERE id=?", (r[0],))
        return {"shortlisted": self.db.execute("SELECT count(*) FROM publications WHERE shortlisted=1").fetchone()[0],
                "pilot": self.db.execute("SELECT count(*) FROM publications WHERE pilot=1").fetchone()[0]}

    def metrics(self):
        statuses = dict(self.db.execute("SELECT status,count(*) FROM publications GROUP BY status").fetchall())
        skips = {}
        for r in self.db.execute("SELECT data FROM publications WHERE status='Skipped'"):
            reason = json.loads(r[0]).get("skip_reason") or "Unspecified"
            skips[reason] = skips.get(reason, 0) + 1
        return {"imported_publications": self.db.execute("SELECT count(*) FROM publications").fetchone()[0],
                "shortlisted_publications": self.db.execute("SELECT count(*) FROM publications WHERE shortlisted=1").fetchone()[0],
                "research_completed": self.db.execute("SELECT count(DISTINCT publication_id) FROM articles").fetchone()[0],
                "research_failed_attempts": self.db.execute("SELECT count(*) FROM events WHERE kind='research_failed'").fetchone()[0],
                "drafts_generated": self.db.execute("SELECT count(*) FROM drafts").fetchone()[0],
                "generation_failed_attempts": self.db.execute("SELECT count(*) FROM events WHERE kind='generation_failed'").fetchone()[0],
                "drafts_approved": statuses.get("Approved", 0), "statuses": statuses, "skips_by_reason": skips,
                "runs": [dict(r) | {"usage": json.loads(r["usage"])} for r in self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 20")]}
