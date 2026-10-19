# Substack outreach

A local research and drafting workspace for personalised Substack outreach. It imports a
workbook of publications, shortlists them, records article evidence, drafts a short email per
publication, and tracks review and approval.

**It never sends anything.** There is no mail transport in this repository. Sending is manual:
you copy an approved draft into your own mail client. Every record stays in a SQLite file on
your machine.

## What runs where

| Piece | Location |
|---|---|
| Worker: import, shortlist, evidence, drafting, approval, export | `substack_outreach/*.py` |
| Review UI (browser) | `substack_outreach/ui.js`, `ui.css`, hosted by `index.template.html` |
| Facts registry (placeholder) | `substack_outreach/facts.example.json` |
| Tests | `tests/test_substack.py`, `tests/substack-ui.test.mjs` |
| Full workflow guide | [`docs/SUBSTACK_OUTREACH.md`](docs/SUBSTACK_OUTREACH.md) |

Python 3.12+ standard library only — no pip packages, no SDK, no API key needed to run locally.
Node 18+ is only needed for the JavaScript tests.

## Setup

```bash
py -3.12 -m venv .venv-substack
.venv-substack\Scripts\python -m substack_outreach --help    # Windows
python build.py                                              # build public/index.html
python -m substack_outreach --config substack.config.json serve
```

Then open <http://127.0.0.1:8765/>. The server binds to loopback only, requires a same-origin
token for every mutation, and serves nothing but the built page. Stop it with Ctrl+C.

Create `substack.config.json` by copying `substack_outreach/config.example.json` and setting your
sender identity, `input_path` and `registry_path`. It is gitignored.

## Common commands

```bash
python -m substack_outreach --config substack.config.json pilot            # import + shortlist + draft
python -m substack_outreach --config substack.config.json process --batch 20
python -m substack_outreach --config substack.config.json research-import articles.json
python -m substack_outreach --config substack.config.json export --format csv --scope pilot --out out.csv
python -m substack_outreach --config substack.config.json metrics
```

## The facts registry

Drafts may only assert facts listed in a registry, and only entries marked `approved` reach an
email. `substack_outreach/facts.example.json` ships **placeholder text** so the project runs and
the tests pass.

Replace it with your own `substack_outreach/facts.json` — gitignored, so your real product
claims, benchmark numbers and launch timing never enter version control — and point
`registry_path` at it. Keep `launch_status` as `unconfirmed` until a release is actually
confirmed; unapproved or expired entries block approval by design.

## What is deliberately not in this repository

The source workbook (`*.xlsx`), the SQLite database, exports, drafts and the real facts registry
are all gitignored. They contain publication contact data and unreleased product information.
Check `.gitignore` before adding files, and keep contact data out of commits.

## Tests

```bash
python -m unittest discover -s tests -p test_substack.py
npm test
```
