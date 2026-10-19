from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .common import digest, dumps, now
from .evidence import BASE_CLAIMS
from .sources import NoRedirect

INSTRUCTIONS = Path(__file__).with_name("writing_instructions.txt").read_text(encoding="utf-8")
SCHEMA = {"type": "object", "properties": {"subject": {"type": "string"}, "plain_text_body": {"type": "string"},
          "email_detail": {"type": "string"}, "claim_ids_used": {"type": "array", "items": {"type": "string"}}},
          "required": ["subject", "plain_text_body", "email_detail", "claim_ids_used"], "additionalProperties": False}


def writing_input(pub, evidence, facts, sender):
    selected = set(evidence["selected_claim_ids"])
    approved = [{k: e[k] for k in ("id", "text", "scope", "email_phrase") if k in e}
                for e in facts["entries"] if e["id"] in selected and e["approval_status"] == "approved"
                and (not e.get("expiry_or_review_date") or e["expiry_or_review_date"] >= now()[:10])]
    return {"recipient": {"first_name": pub.get("confirmed_first_name") if pub.get("name_source") else None},
            "sender": sender, "article": {"title": evidence["article_title"], "url": evidence["article_url"],
            "excerpt": evidence["supporting_excerpt"]},
            "pitch_angle": {"connection": evidence["drex_connection"], "suggested_ask": evidence["suggested_ask"],
                            "exploratory": evidence["fit_category"] == "Exploratory editorial fit"},
            "approved_facts": approved, "confirmed_launch_status": facts["launch_status"],
            "previous_contact": [{k: c.get(k) for k in ("channel", "event", "at")} for c in pub.get("previous_contact", [])]}


class MockWriter:
    name = "mock"

    def generate(self, payload, evidence, revision=None):
        sender = payload["sender"]["name"]
        first = payload["recipient"]["first_name"]
        detail = evidence.get("email_detail") or " ".join(evidence["supporting_excerpt"].split()[:16]).rstrip(".")
        opening = evidence.get("mock_opening") or f"Your article ({evidence['article_url']}) notes: {detail}. {evidence['drex_connection']}"
        para2 = f"I'm {sender} from Nace AI, working on Drex Max, a multimodal decision model for browser-use agents making decisions from screenshots."
        para3 = evidence["suggested_ask"] + " We can offer API credits and answer technical questions."
        body = "\n\n".join([f"Hi {first}," if first else "Hello,", opening, para2, para3,
                            "Explore Drex: https://nace.ai/developers", f"Best,\n{sender}"])
        return {"subject": evidence.get("mock_subject") or "Your article and Drex Max", "plain_text_body": body,
                "email_detail": detail, "claim_ids_used": list(BASE_CLAIMS)}


class OpenAIWriter:
    name = "openai"

    def __init__(self, store, config, run):
        self.store, self.config, self.run = store, config, run
        # A key alone is never authorization. All controls are local worker settings.
        if not config["allow_paid_calls"] or config["budget_usd_per_run"] <= 0:
            raise ValueError("Paid calls require explicit allow_paid_calls=true and a positive per-run budget")
        if not os.environ.get("OPENAI_API_KEY") or not config["model"]:
            raise ValueError("Configure OPENAI_API_KEY in the worker environment and model in the local config")
        for k in ("input_usd_per_million", "output_usd_per_million"):
            if not isinstance(config.get(k), (int, float)) or config[k] <= 0:
                raise ValueError("Set current provider pricing for input and output to enforce the budget")

    def generate(self, payload, evidence, revision=None):
        ids = {e["id"] for e in payload["approved_facts"]}
        if not set(BASE_CLAIMS).issubset(ids):
            raise ValueError("Approve the relevant product, use-case, API-credit, technical-answer and developer-link entries before live writing")
        if not payload["recipient"]["first_name"]:
            raise ValueError("Live writing requires a confirmed public first name; use a manually reviewed neutral greeting otherwise")
        data = {"model": self.config["model"], "store": False, "instructions": INSTRUCTIONS,
                "input": [{"role": "user", "content": dumps(payload)}],
                "max_output_tokens": self.config["max_output_tokens"],
                "text": {"format": {"type": "json_schema", "name": "outreach_email", "strict": True, "schema": SCHEMA}}}
        if revision:
            data["input"].append({"role": "user", "content": dumps({"revision_request": revision})})
        body = dumps(data).encode("utf-8")
        key = "model:v2:" + digest(data)
        cached = self.store.db.execute("SELECT data FROM cache WHERE key=?", (key,)).fetchone()
        if cached:
            return json.loads(cached[0])
        # UTF-8 byte count + overhead is a conservative input-token reservation.
        reserve = ((len(body) + 4096) * self.config["input_usd_per_million"] + self.config["max_output_tokens"] * self.config["output_usd_per_million"]) / 1_000_000
        opener = urllib.request.build_opener(NoRedirect())
        for attempt in range(self.config["max_retries"] + 1):
            self.run.request(reserve)
            try:
                request = urllib.request.Request("https://api.openai.com/v1/responses", data=body,
                    headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "Content-Type": "application/json"})
                with opener.open(request, timeout=45) as response:
                    raw = response.read(2_000_001)
                    if len(raw) > 2_000_000:
                        raise ValueError("Model response exceeded response limit")
                    result = json.loads(raw)
                usage = result.get("usage")
                measured = {"provider": "openai", "model": result.get("model"), "request_id": result.get("id"),
                            "usage": usage, "actual_cost_usd": None, "cost_basis": "Provider did not return billed cost"}
                if usage:
                    measured["estimated_cost_usd"] = (usage.get("input_tokens", 0) * self.config["input_usd_per_million"] + usage.get("output_tokens", 0) * self.config["output_usd_per_million"]) / 1_000_000
                    measured["estimate_basis"] = "Observed tokens times configured rates, without cached-input discounts; not billed cost"
                self.run.usage.append(measured)
                self.run.flush()
                if result.get("status") != "completed":
                    raise ValueError("Model response incomplete or refused; human review required")
                chunks = [c["text"] for o in result.get("output", []) if o.get("type") == "message" for c in o.get("content", []) if c.get("type") == "output_text"]
                if not chunks:
                    raise ValueError("Model returned no draft (possibly a refusal)")
                draft = json.loads("".join(chunks))
                if set(draft) != set(SCHEMA["required"]) or not all(isinstance(draft[k], str) for k in ("subject", "plain_text_body", "email_detail")) or not isinstance(draft["claim_ids_used"], list):
                    raise ValueError("Model output did not match the expected schema")
                self.store.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)", (key, dumps(draft), now()))
                self.store.db.commit()
                return draft
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.config["max_retries"]:
                    raise ValueError(f"Writing provider HTTP {exc.code}; response body omitted to protect credentials") from None
                delay = exc.headers.get("Retry-After", "")
                if delay and (not delay.isdigit() or int(delay) > 20):
                    raise ValueError("Provider rate limit: retry later") from None
                time.sleep(max(int(delay or 0), 2 ** attempt))
            except (urllib.error.URLError, TimeoutError):
                # Ambiguous requests keep their full spending reservation.
                if attempt == self.config["max_retries"]:
                    raise ValueError("Writing provider unavailable after bounded retries") from None
                time.sleep(2 ** attempt)
        raise ValueError("Writing provider unavailable")
