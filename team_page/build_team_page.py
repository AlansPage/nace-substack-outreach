"""Build the shared team page (a claude.ai Artifact) from the Substack JSON export.

The page bakes in every Substack writer from the export, ranked by the workspace's fit triage, with the top 250
marked as the shortlist. Claims, contact records and saved drafts live in the Artifact's shared database, not in
the page. The output contains the private workbook's rows, so it goes to .local/ (gitignored), never public/.

    py -3.12 team_page/build_team_page.py D:\\substack-outreach-private\\substack.json
    py -3.12 team_page/build_team_page.py D:\\substack-outreach-private\\substack.json --out .local\\team-page.html

Publish the output as an Artifact with capabilities
{"db": {"rules": [{"path": "settings", "read": "view", "write": "admin"}]}, "user": {"scopes": ["profile"]}, "sample": {}}
and set the pitch the drafts use in the database document settings/pitch: {product, useCase, offer, link}.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from substack_outreach.common import classify, publication_url  # noqa: E402
from substack_outreach.store import dnc_match, load_dnc  # noqa: E402
from substack_outreach.workbook import identifier  # noqa: E402

FIT = {"Strong technical fit": "S", "Relevant industry/application fit": "R", "Exploratory editorial fit": "E",
       "Unrelated": "U", "Insufficient information": "I"}
SHORTLIST = 250


def writers(table, dnc):
    """One entry per publication (pub_id, else URL), ranked by fit score then workbook order."""
    seen, rows = set(), []
    for r in table["rows"]:
        url, pid = publication_url(r.get("url")), identifier(r.get("pub_id"))
        key = ("p" + pid) if pid else ("u" + hashlib.sha1(url.encode()).hexdigest()[:16]) if url else None
        if not key or key in seen:
            continue
        seen.add(key)
        fit, score, reasons = classify(r)
        entry, _ = dnc_match(dnc, {"publication_url": url, "author_name": r.get("author_name"),
                                   "publication": r.get("name"), "source": r})
        try:
            subs = int(float(r["free_subscribers"])) if r.get("free_subscribers") not in (None, "") else None
        except ValueError:
            subs = None
        rows.append({"key": key, "order": r.get("_row", len(rows) + 2), "score": score, "fit": FIT[fit],
                     "signals": reasons[0].removeprefix("Local metadata signals: ") if reasons[0].startswith("Local") else "",
                     "name": (r.get("name") or "").strip(), "author": (r.get("author_name") or "").strip(), "url": url or "",
                     "subs": subs, "lang": r.get("language") or "", "dnc": 1 if entry else 0,
                     "desc": re.sub(r"\s+", " ", str(r.get("hero_text") or "")).strip()[:180]})
    rows.sort(key=lambda w: (-w["score"], w["order"]))
    taken = 0
    for w in rows:
        w["short"] = 1 if w["score"] > 0 and not w["dnc"] and taken < SHORTLIST else 0
        taken += w["short"]
    return rows


def render(rows):
    # Field order matches the page script: key, publication, author, url, subscribers, fit, signals,
    # description, language, doNotContact, shortlist.
    data = json.dumps([[w["key"], w["name"], w["author"], w["url"], w["subs"], w["fit"], w["signals"], w["desc"],
                        w["lang"], w["dnc"], w["short"]] for w in rows], ensure_ascii=False, separators=(",", ":"))
    # The data sits inside a <script> element, which a literal "</" could close early.
    data = data.replace("</", "<" + chr(92) + "/")
    return (ROOT / "team_page" / "template.html").read_text(encoding="utf-8").replace("__WRITERS__", data)


def main():
    parser = argparse.ArgumentParser(description="Build the team outreach page from the Substack JSON export.")
    parser.add_argument("input", help="Substack-only JSON export: {\"sheets\": {\"substack\": {\"headers\", \"rows\"}}}")
    parser.add_argument("--out", default=str(ROOT / ".local" / "substack-outreach-team.html"))
    parser.add_argument("--dnc", default=str(ROOT / "substack_outreach" / "dnc.json"))
    args = parser.parse_args()
    out = Path(args.out).resolve()
    public = (ROOT / "public").resolve()
    if out == public or public in out.parents:
        parser.exit(2, "Error: the page holds the workbook's rows; write it outside public/\n")
    dnc_path = Path(args.dnc) if Path(args.dnc).is_file() else ROOT / "substack_outreach" / "dnc.example.json"
    table = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))["sheets"]["substack"]
    rows = writers(table, load_dnc(dnc_path))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(rows), encoding="utf-8")
    print(f"{out} ({out.stat().st_size / 1e6:.2f} MB): {len(rows):,} writers, "
          f"{sum(w['short'] for w in rows)} shortlisted, {sum(w['dnc'] for w in rows)} on the do-not-contact list")


if __name__ == "__main__":
    main()
