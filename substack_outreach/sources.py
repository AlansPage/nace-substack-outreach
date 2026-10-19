from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .common import canonical, digest, dumps, now, publication_url, safe_url

USER_AGENT = "NaceResearchLocal/1.0"
MAX_BYTES = 2_000_000
MAX_TEXT = 16_000


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.feeds, self.hidden = [], [], 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self.hidden += 1
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3"):
            self.parts.append("\n")
        if tag == "link" and "alternate" in attrs.get("rel", "").split() and attrs.get("type") in ("application/rss+xml", "application/atom+xml"):
            self.feeds.append(attrs.get("href", ""))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.hidden = max(0, self.hidden - 1)
        if tag in ("p", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, text):
        if not self.hidden:
            self.parts.append(text)


def strip_html(text):
    p = TextParser()
    p.feed(text or "")
    return "\n".join(" ".join(s.split()) for s in "".join(p.parts).splitlines() if s.strip())


def article_date(value):
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return None
    return d.replace(tzinfo=d.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()


def parse_feed(raw, feed_url, limit=5):
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("DTD/entity declarations are not accepted")
    root = ET.fromstring(raw)
    local = lambda e: e.tag.split("}")[-1]
    if local(root) not in ("rss", "feed", "RDF"):
        raise ValueError("URL did not return an RSS/Atom feed")
    rows = []
    for item in root.iter():
        if local(item) not in ("item", "entry"):
            continue
        fields = {}
        for e in item:
            key = local(e)
            if key == "link" and e.get("rel", "alternate") == "alternate" and e.get("href"):
                fields["link"] = e.get("href")
            elif key == "author":
                fields["author"] = " ".join(e.itertext()).strip()
            elif key not in fields:
                fields[key] = "".join(e.itertext()).strip()
        url = safe_url(urljoin(feed_url, fields.get("link", "")))
        if not url or not fields.get("link"):
            continue
        text = strip_html(fields.get("encoded") or fields.get("content") or fields.get("description") or fields.get("summary") or "")
        rows.append({"title": fields.get("title") or None, "url": canonical(url),
                     "date": article_date(fields.get("published") or fields.get("pubDate") or fields.get("updated")),
                     "byline": fields.get("creator") or fields.get("author") or None,
                     "text": text[:MAX_TEXT], "content_status": "excerpt",
                     "retrieved_at": now(), "source_url": feed_url, "source_kind": "publisher_rss",
                     "flags": ["RSS_COMPLETENESS_UNCONFIRMED"] + (["TEXT_TRUNCATED"] if len(text) > MAX_TEXT else [])})
    # Feed order is not assumed chronological. Missing/invalid dates stay unknown.
    rows.sort(key=lambda a: a["date"] or "", reverse=True)
    seen, result = set(), []
    for row in rows:
        if row["url"] not in seen:
            result.append(row)
            seen.add(row["url"])
        if len(result) >= min(limit, 5):
            break
    return result


class Run:
    def __init__(self, store, config, kind):
        self.store, self.config = store, config
        self.id = store.db.execute("INSERT INTO runs(kind,started_at) VALUES(?,?)", (kind, now())).lastrowid
        store.db.commit()
        self.requests, self.reserved, self.usage = 0, 0.0, []

    def request(self, reserve=0):
        if self.requests >= self.config["max_requests_per_run"]:
            raise ValueError("Per-run request limit reached")
        if reserve and self.reserved + reserve > self.config["budget_usd_per_run"]:
            raise ValueError("Per-run spending reservation limit reached")
        self.requests += 1
        self.reserved += reserve
        self.flush()

    def flush(self):
        self.store.db.execute("UPDATE runs SET requests=?,reserved_usd=?,usage=? WHERE id=?", (self.requests, self.reserved, dumps(self.usage), self.id))
        self.store.db.commit()

    def finish(self, error=None):
        self.flush()
        self.store.db.execute("UPDATE runs SET finished_at=?,error=? WHERE id=?", (now(), error, self.id))
        self.store.db.commit()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_https(url):
    u = urlsplit(safe_url(url))
    if u.scheme != "https" or not u.hostname:
        raise ValueError("Collection requires a public HTTPS URL")
    if "/api/" in u.path.lower() or u.hostname == "substack.com":
        raise ValueError("Profile and API endpoints are not research sources")
    addresses = socket.getaddrinfo(u.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Local/private network destinations are not allowed")


class Collector:
    def __init__(self, store, config, run):
        self.store, self.config, self.run = store, config, run
        self.last_request = 0.0
        self.opener = urllib.request.build_opener(NoRedirect())
        self.robots = {}
        self.memory = {}

    def permission(self, pub):
        p = pub.get("research", {})
        if not self.config["network_enabled"]:
            raise ValueError("NETWORK_DISABLED: import supplied research or explicitly enable permitted RSS collection")
        if not p.get("automated_use_permitted") or p.get("source_kind") != "publisher_rss" or not p.get("permission_basis") or not safe_url(p.get("permission_source_url")):
            raise ValueError("SOURCE_PERMISSION_REQUIRED: record publisher permission/terms and source URL first")
        expiry = p.get("permission_review_date")
        if not expiry or expiry < now()[:10]:
            raise ValueError("SOURCE_PERMISSION_REVIEW_REQUIRED")
        if not publication_url(pub.get("publication_url")):
            raise ValueError("PUBLICATION_URL_UNRESOLVED")
        return p

    def get(self, url, allowed_host, use_cache=True):
        public_https(url)
        if urlsplit(url).hostname != allowed_host:
            raise ValueError("Cross-domain feed/redirect requires separate permission and manual import")
        key = "fetch:" + digest(url)
        persist_raw = urlsplit(url).path == '/robots.txt'
        cached = self.store.db.execute("SELECT * FROM cache WHERE key=?", (key,)).fetchone() if persist_raw else None
        if use_cache and key in self.memory:
            return self.memory[key]
        if use_cache and cached and (datetime.now(timezone.utc) - datetime.fromisoformat(cached["at"])).total_seconds() < self.config["cache_hours"] * 3600:
            return json.loads(cached["data"]).encode("utf-8")
        for attempt in range(self.config["max_retries"] + 1):
            time.sleep(max(0, max(1, self.config["request_interval_seconds"]) - (time.monotonic() - self.last_request)))
            self.run.request()
            self.last_request = time.monotonic()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, text/html, text/plain"})
                with self.opener.open(req, timeout=20) as response:
                    raw = response.read(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES:
                        raise ValueError("Source exceeds 2 MB limit")
                    text = raw.decode("utf-8-sig", errors="replace")
                    self.memory[key] = text.encode('utf-8')
                    if persist_raw:
                        self.store.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)", (key, dumps(text), now()))
                        self.store.db.commit()
                    return text.encode("utf-8")
            except urllib.error.HTTPError as e:
                # Do not retry access barriers, redirects, rate limits or CAPTCHAs.
                if e.code not in (500, 502, 503, 504) or attempt == self.config["max_retries"]:
                    raise ValueError(f"HTTP_{e.code}: collection stopped; review source/access restrictions") from None
            except (urllib.error.URLError, TimeoutError):
                if attempt == self.config["max_retries"]:
                    raise ValueError("Source unavailable after bounded retries") from None
            time.sleep(min(2 ** attempt, 4))
        raise ValueError("Source unavailable")

    def allowed_get(self, url, host):
        if host not in self.robots:
            raw = self.get(f"https://{host}/robots.txt", host)
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(raw.decode("utf-8").splitlines())
            self.robots[host] = parser
        rules = self.robots[host]
        if not rules.can_fetch(USER_AGENT, url):
            raise ValueError("ROBOTS_DISALLOWED")
        delay = rules.crawl_delay(USER_AGENT) or 0
        rate = rules.request_rate(USER_AGENT)
        delay = max(delay, rate.seconds / rate.requests if rate else 0)
        if delay > 30:
            raise ValueError("Publisher crawl delay exceeds local worker bound; use a supplied export")
        time.sleep(max(0, delay - (time.monotonic() - self.last_request)))
        return self.get(url, host)

    def collect(self, pub):
        permission = self.permission(pub)
        root = pub["publication_url"]
        host = urlsplit(root).hostname
        feed = permission.get("feed_url")
        if not feed:
            if host.endswith(".substack.com"):
                feed = root.rstrip("/") + "/feed"  # Candidate, verified by parsing below.
            else:
                page = self.allowed_get(root, host).decode("utf-8")
                parser = TextParser()
                parser.feed(page)
                feed = next((urljoin(root, x) for x in parser.feeds if urlsplit(urljoin(root, x)).hostname == host), None)
                if not feed:
                    raise ValueError("FEED_UNDISCOVERED: supply a publisher-linked feed URL")
        if urlsplit(feed).hostname != host or not safe_url(feed):
            raise ValueError("Feed must be a publisher-provided URL on the permitted publication host")
        cache_key = "rss:" + digest([feed, permission, self.config['max_articles']])
        cached = self.store.db.execute("SELECT * FROM cache WHERE key=?", (cache_key,)).fetchone()
        if cached and (datetime.now(timezone.utc) - datetime.fromisoformat(cached['at'])).total_seconds() < self.config['cache_hours'] * 3600:
            return json.loads(cached['data'])
        rows = parse_feed(self.allowed_get(feed, host), feed, self.config["max_articles"])
        if not rows:
            raise ValueError("FEED_EMPTY_OR_UNAVAILABLE")
        for a in rows:
            a["permission_basis"] = permission["permission_basis"]
            a["permission_source_url"] = permission["permission_source_url"]
            a["relationship_source"] = feed
            a["membership_confirmed"] = canonical(a["url"]).startswith(canonical(root) + "/")
        # Cache only the five selected excerpts, not the whole publisher feed/page.
        self.store.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)", (cache_key, dumps(rows), now()))
        self.store.db.commit()
        return rows


def save_articles(store, pub, articles, supplied=False):
    if pub['status'] in ('Sent', 'Replied', 'Do not contact'):
        raise ValueError('Research snapshots for sent/replied or suppressed contacts are immutable')
    if len(articles) > 5:
        raise ValueError("Import at most five recent accessible articles per publication")
    for a in articles:
        if not safe_url(a.get("url")) or not a.get("text"):
            raise ValueError("Article requires canonical URL and available text")
        if supplied and (not a.get("permission_basis") or a.get("source_kind") not in ("manual", "user_export", "user_fixture")):
            raise ValueError("Supplied research requires permission_basis and source_kind: manual, user_export or user_fixture; API-derived data is not accepted")
        if supplied and not a.get("source_url"):
            raise ValueError("Supplied research requires its source_url")
        a = dict(a)
        a["url"] = canonical(a["url"])
        original = a["text"]
        a["text"] = original[:MAX_TEXT]
        a["content_status"] = a.get("content_status", "excerpt")
        if a["content_status"] not in ("complete", "excerpt"):
            raise ValueError("content_status must be complete or excerpt")
        if len(original) > MAX_TEXT:
            a["content_status"] = "excerpt"
            a["flags"] = list(set(a.get("flags", []) + ["TEXT_TRUNCATED"]))
        a["retrieved_at"] = a.get("retrieved_at") or now()
        a["date"] = article_date(a.get("date"))
        a.setdefault("byline", None)
        a.setdefault("title", None)
        a.setdefault("membership_confirmed", False)
        # Resuming an unchanged import must not invalidate an approval.
        old = store.db.execute("SELECT data FROM articles WHERE publication_id=? AND url=?", (pub["id"], a["url"])).fetchone()
        comparable = lambda x: {k: v for k, v in x.items() if k != "retrieved_at"}
        if old and comparable(json.loads(old[0])) == comparable(a):
            continue
        store.db.execute("INSERT INTO articles(publication_id,url,data) VALUES(?,?,?) ON CONFLICT(publication_id,url) DO UPDATE SET data=excluded.data", (pub["id"], a["url"], dumps(a)))
        store.invalidate(pub["id"], "Research changed")
        if pub["status"] in ("Approved", "Reviewed"):
            pub["status"] = "Drafted"
        pub["evidence"] = None
    # Retain only the five most recent records needed for this research task.
    rows = store.db.execute("SELECT id,data FROM articles WHERE publication_id=?", (pub["id"],)).fetchall()
    ordered = sorted(rows, key=lambda r: json.loads(r["data"]).get("date") or "", reverse=True)
    for r in ordered[5:]:
        store.db.execute("DELETE FROM articles WHERE id=?", (r["id"],))
    if pub["status"] in ("Imported", "Shortlisted", "Needs research"):
        pub["status"] = "Researched"
    store.save(pub)
    store.event(pub["id"], "research_completed", {"articles": len(articles), "source": "supplied" if supplied else "rss"})
    store.db.commit()
