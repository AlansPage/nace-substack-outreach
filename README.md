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
| Do-not-contact seed list | `substack_outreach/dnc.example.json` (copy to gitignored `dnc.json`) |
| Gmail reply-to export (runs in Google Apps Script) | `scripts/gmail_replyto.gs` |
| Tests and saved fixtures | `tests/test_substack.py`, `tests/*.test.mjs`, `tests/fixtures/` |
| Full workflow guide | [`docs/SUBSTACK_OUTREACH.md`](docs/SUBSTACK_OUTREACH.md) |

Python 3.12+ standard library only — no pip packages, no SDK, no API key needed to run locally
(the optional `local` writer talks to a model server on your own machine).
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
sender identity, `input_path` and `registry_path`. It is gitignored. Copy
`substack_outreach/dnc.example.json` to `substack_outreach/dnc.json` (also gitignored) to keep your own
do-not-contact list.

## Common commands

In PowerShell, from the repository folder:

```powershell
py -3.12 -m substack_outreach --config .\substack.config.json pilot                     # import + DNC list + shortlist + draft
py -3.12 -m substack_outreach --config .\substack.config.json process --batch 20
py -3.12 -m substack_outreach --config .\substack.config.json research-import .\.local\articles.json
py -3.12 -m substack_outreach --config .\substack.config.json collect-contacts --batch 20  # about-page proposals (needs collect_contacts + network_enabled)
py -3.12 -m substack_outreach --config .\substack.config.json contacts-import .\.local\substack-replyto.json   # Gmail reply-to export
py -3.12 -m substack_outreach --config .\substack.config.json export --format csv --scope pilot --out .\.local\pilot.csv
py -3.12 -m substack_outreach --config .\substack.config.json export --format sendsheet --out .\.local\sendsheet.html   # approved drafts only
py -3.12 -m substack_outreach --config .\.local\hosted.config.json facts-push .\substack_outreach\facts.json   # hosted shared database only
py -3.12 -m substack_outreach --config .\substack.config.json metrics
```

Contact proposals and imported reply-to routes are never confirmed automatically: a named person confirms
each one in the UI before approval is possible. The send sheet only fills your mail client; it sends nothing.

## Configuration keys added since the first release

| Key | Default | What it does |
|---|---|---|
| `dnc_path` | `substack_outreach/dnc.json` | Do-not-contact list applied at every import; falls back to the committed seed |
| `default_feed_permission` | `null` | Team policy decision: research permission for workbook Substack rows without their own `research` block |
| `collect_contacts` | `false` | Allows `collect-contacts` to read `{publication_url}/about` pages (also needs `network_enabled`) |
| `provider` | `mock` | `mock`, `openai` or `local` |
| `local_base_url` | `http://127.0.0.1:11434` | Loopback-only OpenAI-compatible endpoint for `provider: "local"` (for example Ollama) |

See [`docs/SUBSTACK_OUTREACH.md`](docs/SUBSTACK_OUTREACH.md) for each one.

## Team page (shared on claude.ai)

`team_page/` builds the page the team uses to split the work: every Substack writer from the export, ranked, with
**Claim**, **Mark contacted** and **Draft email** (Claude). Claims and contact records are shared live through the
Artifact's database, so nobody writes to the same creator twice. The built page contains the workbook's rows, so it
is written to `.local\` and never committed:

```powershell
py -3.12 team_page\build_team_page.py D:\substack-outreach-private\substack.json
```

Publish the output as a private Artifact (capabilities in the script's docstring) and share it with teammates as
Contributors. The drafts use the pitch stored in the Artifact's `settings/pitch` document.

## Hosted team workspace (optional)

The same code deploys to Vercel for a shared workspace: Google sign-in, a Neon Postgres database
(`pg8000` is the only package, used only in hosted mode), and private inputs pushed from your machine
with `facts-push` and `pilot`. **Sign-in is open to any Google account unless you set
`ALLOWED_EMAIL_DOMAIN` or `ALLOWED_EMAILS`**, and everyone signed in can read facts and contact routes.
With `TEAM_PASSWORD` set, sign-in is a name plus one shared team password instead, with no Google setup.
Setup steps are in [`docs/SUBSTACK_OUTREACH.md`](docs/SUBSTACK_OUTREACH.md#hosted-team-workspace-vercel).

| Hosted piece | Location |
|---|---|
| Vercel entry point (WSGI `app`) | `app.py` → `substack_outreach/hosted.py` |
| Google sign-in and sessions | `substack_outreach/auth.py` |
| SQLite / Postgres storage | `substack_outreach/db.py` |
| Entry point, dependency and upload filter | `pyproject.toml` (`[tool.vercel] entrypoint`), `.python-version`, `.vercelignore` |

## The facts registry

Drafts may only assert facts listed in a registry, and only entries marked `approved` reach an
email. `substack_outreach/facts.example.json` ships **placeholder text** so the project runs and
the tests pass.

Replace it with your own `substack_outreach/facts.json` — gitignored, so your real product
claims, benchmark numbers and launch timing never enter version control — and point
`registry_path` at it. Keep `launch_status` as `unconfirmed` until a release is actually
confirmed; unapproved or expired entries block approval by design.

## What is deliberately not in this repository

The source workbook (`*.xlsx`), the SQLite database, exports (including the send sheet), drafts, the
real facts registry and your `dnc.json` are all gitignored. They contain publication contact data and unreleased product information.
Check `.gitignore` before adding files, and keep contact data out of commits.

## Tests

```bash
python -m unittest discover -s tests -p test_substack.py
npm test
```
