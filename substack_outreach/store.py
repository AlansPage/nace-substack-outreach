from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .common import canonical, classify, digest, dumps, is_profile, now, publication_url
from .workbook import EXPECTED, identifier, read_workbook


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS publications (
          id INTEGER PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Imported',
          score INTEGER NOT NULL DEFAULT 0, shortlisted INTEGER NOT NULL DEFAULT 0,
          pilot INTEGER NOT NULL DEFAULT 0, author_key TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS aliases (key TEXT PRIMARY KEY, publication_id INTEGER NOT NULL REFERENCES publications(id));
        CREATE TABLE IF NOT EXISTS imports (fingerprint TEXT PRIMARY KEY, path TEXT, headers TEXT, at TEXT);
        CREATE TABLE IF NOT EXISTS source_rows (fingerprint TEXT, row_number INTEGER, publication_id INTEGER,
          data TEXT NOT NULL, PRIMARY KEY(fingerprint,row_number));
        CREATE TABLE IF NOT EXISTS articles (id INTEGER PRIMARY KEY, publication_id INTEGER NOT NULL REFERENCES publications(id),
          url TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(publication_id,url));
        CREATE TABLE IF NOT EXISTS contacts (id INTEGER PRIMARY KEY, author_key TEXT NOT NULL,
          contact_key TEXT, channel TEXT, event TEXT, at TEXT, source TEXT, note TEXT, dedup TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS suppressions (key TEXT PRIMARY KEY, reason TEXT, at TEXT);
        CREATE TABLE IF NOT EXISTS identities (alias TEXT PRIMARY KEY, author_key TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS drafts (id INTEGER PRIMARY KEY, publication_id INTEGER NOT NULL REFERENCES publications(id),
          version INTEGER NOT NULL, data TEXT NOT NULL, hash TEXT NOT NULL, approved_hash TEXT, approved_by TEXT,
          approved_at TEXT, created_at TEXT, UNIQUE(publication_id,version));
        CREATE TABLE IF NOT EXISTS registry_history (hash TEXT PRIMARY KEY, version INTEGER, data TEXT, at TEXT);
        CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, kind TEXT, started_at TEXT, finished_at TEXT,
          requests INTEGER DEFAULT 0, reserved_usd REAL DEFAULT 0, usage TEXT DEFAULT '[]', error TEXT);
        CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, data TEXT NOT NULL, at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, publication_id INTEGER, kind TEXT, detail TEXT, at TEXT);
        CREATE INDEX IF NOT EXISTS pub_queue ON publications(pilot,score);
        CREATE INDEX IF NOT EXISTS pub_author ON publications(author_key);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def event(self, pid, kind, detail):
        self.db.execute("INSERT INTO events(publication_id,kind,detail,at) VALUES(?,?,?,?)", (pid, kind, dumps(detail), now()))

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
        self.db.execute("INSERT OR IGNORE INTO contacts(author_key,contact_key,channel,event,at,source,note,dedup) VALUES(?,?,?,?,?,?,?,?)",
                        (data["author_key"], self.contact_key(data), channel, event, at or now(), source, note, dedup))
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
